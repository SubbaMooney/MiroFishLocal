"""
Smoke-Tests Phase 1 (LightRAG-Migration).

Verifiziert lightrag_factory + RagManager mit GEMOCKTEN LLM-/Embedding-
Funktionen — kein Egress, keine API-Keys noetig. Aequivalent zum
backend/scripts/lightrag_mock_spike.py, aber als pytest-Suite und gegen
die produktiven app/services/-Module.

Was getestet wird:
  - lightrag_factory: Validation-Fehler wenn ENV-Vars fehlen
  - RagManager: Singleton, Loop-Thread, get_or_create, insert, query,
    delete, shutdown — alles mit gepatchter create_rag-Factory.

Was NICHT getestet wird:
  - Echte Bailian/OpenAI-Roundtrips (siehe lightrag_real_spike.py).
  - LightRAG-Output-Qualitaet — das ist Sache des Echt-Spikes.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.services import rag_manager as rm_module
from app.services.rag_manager import RagManager


# ---------------------------------------------------------------------------
# Mock-LLM/Embedding (deterministisch, keine echten API-Calls)
# ---------------------------------------------------------------------------

TUPLE_D = "<|#|>"
COMPLETE_D = "<|COMPLETE|>"


async def _mock_llm(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list | None = None,
    keyword_extraction: bool = False,
    **kwargs: Any,
) -> str:
    """Synthetischer LLM-Output, akzeptiert alle LightRAG-Aufruf-Schemata."""
    full = (system_prompt or "") + "\n" + (prompt or "")
    lower = full.lower()
    if keyword_extraction or "keyword" in lower:
        return (
            '{"high_level_keywords": ["test"], "low_level_keywords": ["alice"]}'
        )
    if "entities" in lower or "extract" in lower:
        return (
            f'("entity"{TUPLE_D}"alice"{TUPLE_D}"person"{TUPLE_D}'
            f'"Test-Persona"){COMPLETE_D}'
        )
    return "Synthetische Antwort vom Mock-LLM (keine echten API-Calls)."


async def _mock_embed_inner(texts: list[str]) -> np.ndarray:
    """1024-dim hash-deterministische Embeddings, L2-normiert."""
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
    """Ein eigener working_dir-Base pro Test, automatisch geraeumt."""
    base = tmp_path / "lightrag"
    base.mkdir(parents=True, exist_ok=True)
    yield base
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def patched_rag_factory(monkeypatch):
    """Patcht lightrag_factory.create_rag, sodass RagManager Mock-Funktionen
    statt echter Bailian/OpenAI-Calls verwendet."""
    from app.services import lightrag_factory
    from lightrag import LightRAG
    from lightrag.kg.shared_storage import initialize_pipeline_status
    from lightrag.utils import EmbeddingFunc

    async def _create_rag(working_dir: str):
        embed = EmbeddingFunc(embedding_dim=1024, max_token_size=8192, func=_mock_embed_inner)
        rag = LightRAG(
            working_dir=working_dir,
            llm_model_func=_mock_llm,
            embedding_func=embed,
        )
        await rag.initialize_storages()
        await initialize_pipeline_status()
        return rag

    monkeypatch.setattr(lightrag_factory, "create_rag", _create_rag)
    yield


@pytest.fixture
def manager(tmp_working_dir, patched_rag_factory):
    """RagManager-Instanz mit Mock-Factory + tmp working_dir.

    Wir umgehen das Singleton fuer Tests, damit jeder Test isoliert laeuft
    und am Ende sauber heruntergefahren wird.
    """
    RagManager.reset_singleton()
    mgr = RagManager(working_dir_base=tmp_working_dir)
    yield mgr
    mgr.shutdown()
    RagManager.reset_singleton()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_factory_validation_no_api_key(monkeypatch):
    """create_llm_func soll mit ValueError fehlschlagen wenn LLM_API_KEY leer ist."""
    from app.config import Config
    from app.services import lightrag_factory

    monkeypatch.setattr(Config, "LLM_API_KEY", None, raising=False)
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        lightrag_factory.create_llm_func()


def test_factory_validation_no_model_name(monkeypatch):
    """create_llm_func soll mit ValueError fehlschlagen wenn LLM_MODEL_NAME leer ist."""
    from app.config import Config
    from app.services import lightrag_factory

    monkeypatch.setattr(Config, "LLM_API_KEY", "dummy", raising=False)
    monkeypatch.setattr(Config, "LLM_MODEL_NAME", None, raising=False)
    with pytest.raises(ValueError, match="LLM_MODEL_NAME"):
        lightrag_factory.create_llm_func()


def test_factory_embed_validation_no_model(monkeypatch):
    """create_embed_func soll mit ValueError fehlschlagen wenn EMBED_MODEL_NAME leer ist."""
    from app.config import Config
    from app.services import lightrag_factory

    monkeypatch.setattr(Config, "EMBED_API_KEY", "dummy", raising=False)
    monkeypatch.setattr(Config, "EMBED_MODEL_NAME", None, raising=False)
    with pytest.raises(ValueError, match="EMBED_MODEL_NAME"):
        lightrag_factory.create_embed_func()


def test_manager_singleton(tmp_working_dir, patched_rag_factory):
    """get_instance() liefert dieselbe Instanz."""
    RagManager.reset_singleton()
    try:
        a = RagManager.get_instance()
        b = RagManager.get_instance()
        assert a is b
        assert a._thread.is_alive()
    finally:
        RagManager.reset_singleton()


def test_manager_loop_thread_running(manager: RagManager):
    """Der dedizierte Loop-Thread laeuft."""
    assert manager._thread.is_alive()
    assert manager._loop.is_running()
    assert manager._thread.daemon


def test_manager_insert_and_query(manager: RagManager):
    """Init + Insert + Query gegen eine Mock-Instanz funktionieren end-to-end."""
    graph_id = "test_graph"
    assert not manager.has_instance(graph_id)

    text = (
        "Alice ist Engineering-Lead bei Acme. Bob ist Product-Owner. "
        "Beide arbeiten am Projekt MiroFish."
    )
    manager.insert(graph_id, text)
    assert manager.has_instance(graph_id)

    answer = manager.query(graph_id, "Wer ist Alice?", mode="hybrid")
    assert isinstance(answer, str)
    assert len(answer) > 0


def test_manager_get_all_nodes(manager: RagManager):
    """Strukturierte Graph-Reads (NetworkX) funktionieren ohne LLM-Calls."""
    graph_id = "test_nodes"
    manager.insert(graph_id, "Alice arbeitet mit Bob.")
    nodes = manager.get_all_nodes(graph_id)
    assert isinstance(nodes, list)
    # Mock-LLM liefert Entity 'alice' — wir pruefen nicht den genauen Knoten,
    # nur dass der Reader lebt und die Struktur stimmt.


def test_manager_delete_removes_working_dir(manager: RagManager, tmp_working_dir: Path):
    """delete() entfernt Instanz + Verzeichnis."""
    graph_id = "test_delete"
    manager.insert(graph_id, "Trivialer Insert.")
    assert (tmp_working_dir / graph_id).exists()
    manager.delete(graph_id)
    assert not manager.has_instance(graph_id)
    assert not (tmp_working_dir / graph_id).exists()


def test_manager_shutdown_idempotent(tmp_working_dir, patched_rag_factory):
    """Mehrfaches shutdown() schlaegt nicht fehl."""
    mgr = RagManager(working_dir_base=tmp_working_dir)
    mgr.shutdown()
    mgr.shutdown()  # darf nicht crashen
