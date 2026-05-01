"""
Phase-2 Tests fuer Zep->LightRAG Migration.

Deckt zwei Schichten ab:
  1. ``RagManager.set_ontology`` + Hint-Plumbing (mit gepatcher Mock-Factory).
  2. ``GraphBuilderService`` mit gemocktem RagManager — pure Unit-Tests
     ohne LightRAG-Initialisierung. Validiert vor allem das
     NetworkX->Frontend Schema-Mapping in ``get_graph_data``.

Keine echten LLM-Calls. End-to-End-Validierung gegen Bailian/echtes LLM
ist Aufgabe eines separaten gated Integration-Spikes (out of scope fuer
diesen PR — siehe Migration-Doc Phase 0/2 Acceptance-Test).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services import rag_manager as rm_module
from app.services.graph_builder import (
    GraphBuilderService,
    _edge_get,
    _node_get,
)
from app.services.rag_manager import RagManager, _format_ontology_hint


# ---------------------------------------------------------------------------
# Mock-LLM/Embedding (geteilt mit Phase 1; deterministisch, keine API-Calls)
# ---------------------------------------------------------------------------


async def _mock_llm(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list | None = None,
    keyword_extraction: bool = False,
    **kwargs: Any,
) -> str:
    full = (system_prompt or "") + "\n" + (prompt or "")
    lower = full.lower()
    if keyword_extraction or "keyword" in lower:
        return '{"high_level_keywords": ["test"], "low_level_keywords": ["alice"]}'
    if "entities" in lower or "extract" in lower:
        return '("entity"<|#|>"alice"<|#|>"person"<|#|>"Test-Persona")<|COMPLETE|>'
    return "Synthetische Antwort."


async def _mock_embed_inner(texts: list[str]) -> np.ndarray:
    out = np.zeros((len(texts), 1024), dtype=np.float32)
    for i, t in enumerate(texts):
        seed = int.from_bytes(hashlib.sha256(t.encode("utf-8")).digest()[:4], "little")
        rng = np.random.default_rng(seed)
        v = rng.standard_normal(1024).astype(np.float32)
        n = float(np.linalg.norm(v))
        if n > 0:
            v /= n
        out[i] = v
    return out


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_working_dir(tmp_path: Path) -> Path:
    base = tmp_path / "lightrag-p2"
    base.mkdir(parents=True, exist_ok=True)
    yield base
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def patched_rag_factory(monkeypatch):
    """Patcht create_rag so, dass der hint_provider durchgereicht wird —
    aber der LLM-Func ist gemockt. Wir spiegeln die Hint-Logik der echten
    Factory hier, damit wir die Hint-Injektion verifizieren koennen.
    """
    from app.services import lightrag_factory
    from lightrag import LightRAG
    from lightrag.kg.shared_storage import initialize_pipeline_status
    from lightrag.utils import EmbeddingFunc

    captured_calls: list[dict] = []

    async def _create_rag(working_dir: str, system_prompt_hint_provider=None):
        async def _llm_with_hint(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list | None = None,
            keyword_extraction: bool = False,
            **kwargs: Any,
        ) -> str:
            hint = system_prompt_hint_provider() if system_prompt_hint_provider else ""
            captured_calls.append({
                "hint": hint,
                "system_prompt": system_prompt,
                "prompt": prompt,
            })
            return await _mock_llm(
                prompt,
                system_prompt=(f"{hint}\n\n{system_prompt}" if hint and system_prompt else hint or system_prompt),
                history_messages=history_messages,
                keyword_extraction=keyword_extraction,
                **kwargs,
            )

        embed = EmbeddingFunc(embedding_dim=1024, max_token_size=8192, func=_mock_embed_inner)
        rag = LightRAG(
            working_dir=working_dir,
            llm_model_func=_llm_with_hint,
            embedding_func=embed,
        )
        await rag.initialize_storages()
        await initialize_pipeline_status()
        return rag

    monkeypatch.setattr(lightrag_factory, "create_rag", _create_rag)
    yield captured_calls


@pytest.fixture
def manager(tmp_working_dir, patched_rag_factory):
    RagManager.reset_singleton()
    mgr = RagManager(working_dir_base=tmp_working_dir)
    yield mgr
    mgr.shutdown()
    RagManager.reset_singleton()


# ---------------------------------------------------------------------------
# RagManager: Ontology-Plumbing
# ---------------------------------------------------------------------------


def test_format_ontology_hint_renders_entities_and_edges():
    ontology = {
        "entity_types": [
            {"name": "Person", "description": "A human."},
            {"name": "Organization", "description": "A company."},
        ],
        "edge_types": [
            {
                "name": "works_for",
                "description": "Employment relation.",
                "source_targets": [{"source": "Person", "target": "Organization"}],
            }
        ],
    }
    hint = _format_ontology_hint(ontology)
    assert "Person" in hint
    assert "Organization" in hint
    assert "works_for" in hint
    assert "Person->Organization" in hint
    assert "Schema Hint" in hint


def test_format_ontology_hint_handles_empty_ontology():
    hint = _format_ontology_hint({})
    assert "Schema Hint" in hint
    # Keine Entity/Edge-Sektionen, aber kein Crash.


def test_set_ontology_persists_json_and_registers_hint(manager: RagManager, tmp_working_dir: Path):
    graph_id = "ontology_graph"
    ontology = {
        "entity_types": [{"name": "Person", "description": "A human"}],
        "edge_types": [],
    }

    manager.set_ontology(graph_id, ontology)

    # Persistenz: ontology.json im working_dir
    ontology_file = tmp_working_dir / graph_id / "ontology.json"
    assert ontology_file.exists()
    persisted = json.loads(ontology_file.read_text(encoding="utf-8"))
    assert persisted == ontology

    # Hint im Manager-State
    assert "Person" in manager._ontology_hints[graph_id]


def test_set_ontology_can_be_called_before_first_insert(manager: RagManager):
    """set_ontology vor jedem insert ist der typische Caller-Flow."""
    graph_id = "early_ontology"
    manager.set_ontology(graph_id, {"entity_types": [{"name": "Foo"}], "edge_types": []})
    assert not manager.has_instance(graph_id)  # noch keine Instanz erzeugt
    assert "Foo" in manager._ontology_hints[graph_id]


def test_factory_llm_func_prepends_hint(monkeypatch):
    """Direkter Unit-Test: create_llm_func mit hint_provider prependet den
    Hint live aus dem Provider — kein LightRAG noetig."""
    from app.config import Config
    from app.services import lightrag_factory

    monkeypatch.setattr(Config, "LLM_API_KEY", "dummy", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL_NAME", "test-model", raising=False)

    captured: list[list[dict]] = []

    class _FakeChat:
        def create(self, *, model, messages, **kwargs):
            captured.append(messages)
            class _Choice:
                message = type("M", (), {"content": "ok"})()
            return type("R", (), {"choices": [_Choice()]})()

    class _FakeClient:
        chat = type("C", (), {"completions": _FakeChat()})()

    monkeypatch.setattr(lightrag_factory, "_get_llm_client", lambda: _FakeClient())

    # Mutable Box: Hint kann zwischen Calls wechseln (echter Use-Case in RagManager).
    hint_box = {"v": "INITIAL_HINT"}
    llm = lightrag_factory.create_llm_func(
        system_prompt_hint_provider=lambda: hint_box["v"],
    )

    asyncio.run(llm("user-prompt", system_prompt="orig-system"))
    assert "INITIAL_HINT" in captured[-1][0]["content"]
    assert "orig-system" in captured[-1][0]["content"]

    hint_box["v"] = "UPDATED_HINT"
    asyncio.run(llm("user-prompt", system_prompt="orig-system"))
    assert "UPDATED_HINT" in captured[-1][0]["content"]
    # INITIAL_HINT darf NICHT im zweiten Call auftauchen (Provider wird live gelesen).
    assert "INITIAL_HINT" not in captured[-1][0]["content"]


def test_factory_llm_func_no_hint_when_provider_empty(monkeypatch):
    """Leerer Hint -> System-Prompt unveraendert."""
    from app.config import Config
    from app.services import lightrag_factory

    monkeypatch.setattr(Config, "LLM_API_KEY", "dummy", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL_NAME", "test-model", raising=False)

    captured: list[list[dict]] = []

    class _FakeChat:
        def create(self, *, model, messages, **kwargs):
            captured.append(messages)
            class _Choice:
                message = type("M", (), {"content": "ok"})()
            return type("R", (), {"choices": [_Choice()]})()

    class _FakeClient:
        chat = type("C", (), {"completions": _FakeChat()})()

    monkeypatch.setattr(lightrag_factory, "_get_llm_client", lambda: _FakeClient())

    llm = lightrag_factory.create_llm_func(system_prompt_hint_provider=lambda: "")
    asyncio.run(llm("user-prompt", system_prompt="orig-system"))
    assert captured[-1][0]["content"] == "orig-system"


def test_ontology_restored_from_working_dir(tmp_working_dir, patched_rag_factory):
    """Nach Prozess-Restart: ontology.json wird automatisch wieder eingelesen."""
    graph_id = "restore_graph"
    ontology = {
        "entity_types": [{"name": "RestoredEntity", "description": "x"}],
        "edge_types": [],
    }

    # Erste Instanz: setzt Ontology + persistiert.
    RagManager.reset_singleton()
    mgr1 = RagManager(working_dir_base=tmp_working_dir)
    try:
        mgr1.set_ontology(graph_id, ontology)
    finally:
        mgr1.shutdown()
        RagManager.reset_singleton()

    # Zweite Instanz: simuliert Prozess-Restart, muss Ontology wiederfinden.
    mgr2 = RagManager(working_dir_base=tmp_working_dir)
    try:
        # has_instance ist False (noch nichts geladen), aber _get_or_create
        # lazy-loaded den Hint. Wir triggern via insert.
        mgr2.insert(graph_id, "Test.")
        assert "RestoredEntity" in mgr2._ontology_hints[graph_id]
    finally:
        mgr2.shutdown()
        RagManager.reset_singleton()


def test_delete_clears_ontology_hint(manager: RagManager):
    graph_id = "to_delete"
    manager.set_ontology(graph_id, {"entity_types": [{"name": "X"}], "edge_types": []})
    manager.insert(graph_id, "trivial")
    assert graph_id in manager._ontology_hints

    manager.delete(graph_id)
    assert graph_id not in manager._ontology_hints


# ---------------------------------------------------------------------------
# Defensive Schema-Lookup-Helper
# ---------------------------------------------------------------------------


def test_node_get_dict_form():
    n = {"entity_name": "alice", "entity_type": "PERSON", "description": "x"}
    assert _node_get(n, "entity_name") == "alice"
    assert _node_get(n, "name", "entity_name") == "alice"
    assert _node_get(n, "missing", default="fallback") == "fallback"


def test_node_get_tuple_form():
    n = ("alice", {"entity_type": "PERSON", "description": "x"})
    assert _node_get(n, "id") == "alice"
    assert _node_get(n, "entity_type") == "PERSON"
    assert _node_get(n, "missing", default=None) is None


def test_edge_get_tuple_form_with_endpoints():
    e = (("alice", "bob"), {"description": "knows", "weight": 0.5})
    assert _edge_get(e, "src_id") == "alice"
    assert _edge_get(e, "tgt_id") == "bob"
    assert _edge_get(e, "description") == "knows"
    assert _edge_get(e, "weight") == 0.5


def test_edge_get_dict_form():
    e = {"src_id": "alice", "tgt_id": "bob", "description": "knows"}
    assert _edge_get(e, "src_id") == "alice"
    assert _edge_get(e, "description") == "knows"


# ---------------------------------------------------------------------------
# GraphBuilderService: Unit-Tests mit gemocktem RagManager
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_rag(monkeypatch):
    """Mockt RagManager.get_instance() so, dass GraphBuilderService nichts
    Echtes erzeugt. Zurueck kommt der Mock fuer Assertions."""
    fake = MagicMock()
    fake.get_all_nodes.return_value = []
    fake.get_all_edges.return_value = []
    monkeypatch.setattr(RagManager, "get_instance", classmethod(lambda cls: fake))
    return fake


def test_create_graph_returns_unique_ids(mock_rag):
    builder = GraphBuilderService()
    a = builder.create_graph("Test-Graph A")
    b = builder.create_graph("Test-Graph B")
    assert a != b
    assert a.startswith("mirofish_")
    assert b.startswith("mirofish_")
    # create_graph darf KEINE Cloud/RAG-Calls absetzen — Working-Dir ist lazy.
    mock_rag.insert.assert_not_called()
    mock_rag.set_ontology.assert_not_called()


def test_set_ontology_delegates_to_rag_manager(mock_rag):
    builder = GraphBuilderService()
    ontology = {"entity_types": [{"name": "Person"}], "edge_types": []}
    builder.set_ontology("g1", ontology)
    mock_rag.set_ontology.assert_called_once_with("g1", ontology)


def test_add_text_batches_inserts_each_chunk_returns_empty(mock_rag):
    builder = GraphBuilderService()
    chunks = ["a", "b", "c", "d", "e"]
    progress_calls: list[tuple[str, float]] = []
    result = builder.add_text_batches(
        "g1", chunks, batch_size=2,
        progress_callback=lambda msg, p: progress_calls.append((msg, p)),
    )
    assert result == []  # keine Episode-UUIDs mehr
    assert mock_rag.insert.call_count == 5
    # Progress wird mindestens am Ende gerufen (i == total).
    assert progress_calls, "Progress-Callback sollte aufgerufen werden"
    assert progress_calls[-1][1] == pytest.approx(1.0)


def test_add_text_batches_empty_chunks_no_insert(mock_rag):
    builder = GraphBuilderService()
    result = builder.add_text_batches("g1", [], batch_size=3)
    assert result == []
    mock_rag.insert.assert_not_called()


def test_add_text_batches_propagates_insert_failure(mock_rag):
    mock_rag.insert.side_effect = [None, RuntimeError("LLM down")]
    builder = GraphBuilderService()
    with pytest.raises(RuntimeError, match="LLM down"):
        builder.add_text_batches("g1", ["a", "b", "c"], batch_size=1)


def test_get_graph_info_aggregates_entity_types(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
        {"entity_name": "bob", "entity_type": "PERSON"},
        {"entity_name": "acme", "entity_type": "ORG"},
        {"entity_name": "x", "entity_type": "Entity"},  # generisch -> raus
    ]
    mock_rag.get_all_edges.return_value = [{"src_id": "a", "tgt_id": "b"}]

    builder = GraphBuilderService()
    info = builder._get_graph_info("g1")
    assert info.node_count == 4
    assert info.edge_count == 1
    assert set(info.entity_types) == {"PERSON", "ORG"}


def test_get_graph_data_maps_nodes_to_frontend_schema(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {
            "entity_name": "alice",
            "entity_type": "PERSON",
            "description": "Engineering-Lead",
            "source_id": "chunk_1",
        },
        {
            "entity_name": "acme",
            "entity_type": "ORG",
            "description": "A company",
        },
    ]
    mock_rag.get_all_edges.return_value = [
        {
            "src_id": "alice",
            "tgt_id": "acme",
            "description": "works at",
            "keywords": "employment",
            "weight": 1.0,
            "source_id": "chunk_1",
        }
    ]

    builder = GraphBuilderService()
    data = builder.get_graph_data("g1")

    assert data["graph_id"] == "g1"
    assert data["node_count"] == 2
    assert data["edge_count"] == 1

    # Node-Mapping
    alice = next(n for n in data["nodes"] if n["name"] == "alice")
    assert alice["uuid"] == "alice"
    assert alice["labels"] == ["PERSON"]
    assert alice["summary"] == "Engineering-Lead"
    assert alice["attributes"] == {"source_id": "chunk_1"}
    # Felder, die LightRAG nicht kennt, muessen None sein (Frontend-Erwartung).
    assert alice["created_at"] is None

    # Edge-Mapping
    edge = data["edges"][0]
    assert edge["source_node_uuid"] == "alice"
    assert edge["target_node_uuid"] == "acme"
    assert edge["source_node_name"] == "alice"
    assert edge["target_node_name"] == "acme"
    assert edge["fact"] == "works at"
    assert edge["fact_type"] == "employment"
    assert edge["attributes"]["weight"] == 1.0
    # Pflicht-Defaults fuers Frontend
    for k in ("created_at", "valid_at", "invalid_at", "expired_at"):
        assert edge[k] is None
    assert edge["episodes"] == []


def test_get_graph_data_skips_nodes_without_name(mock_rag):
    """Defensiv: Knoten ohne entity_name werden nicht emittiert
    (Frontend-Renderer crashen sonst auf leeren Labels)."""
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "", "entity_type": "PERSON"},  # leer -> raus
        {"entity_name": "alice", "entity_type": "PERSON"},
    ]
    builder = GraphBuilderService()
    data = builder.get_graph_data("g1")
    assert data["node_count"] == 1
    assert data["nodes"][0]["name"] == "alice"


def test_get_graph_data_handles_tuple_node_format(mock_rag):
    """LightRAG kann Knoten auch als (id, data)-Tuple liefern."""
    mock_rag.get_all_nodes.return_value = [
        ("alice", {"entity_type": "PERSON", "description": "lead"}),
    ]
    mock_rag.get_all_edges.return_value = []
    builder = GraphBuilderService()
    data = builder.get_graph_data("g1")
    assert data["nodes"][0]["name"] == "alice"
    assert data["nodes"][0]["labels"] == ["PERSON"]


def test_delete_graph_delegates_to_rag_manager(mock_rag):
    builder = GraphBuilderService()
    builder.delete_graph("g1")
    mock_rag.delete.assert_called_once_with("g1")
