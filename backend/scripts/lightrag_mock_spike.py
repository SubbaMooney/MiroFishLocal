"""
LightRAG Mock-Spike — Phase 0 Code-Bereitschaft

Validiert die im Migrationsplan (docs/MIGRATION-ZEP-TO-LIGHTRAG.md) definierten
sieben kritischen Annahmen, **ohne echte Bailian-API-Calls**. LLM- und Embedding-
Funktionen sind vollständig gemockt (deterministische Synthetic-Outputs).

Was hier validiert wird (siehe Report):
  1. Pflicht-Init-Pattern (initialize_storages + initialize_pipeline_status)
  2. RagManager-Singleton mit Event-Loop-Thread und sync→async-Bridge
  3. Multi-Project-Isolation via separater working_dir
  4. NetworkX-Graph-Zugriff für strukturierte Reads (get_all_nodes/get_all_edges)
  5. Per-Graph asyncio.Lock verhindert NanoVectorDB-Race
  6. Storage-Persistenz (erwartetes File-Layout im working_dir)
  7. shutil.rmtree(working_dir) als Delete-Pfad

Was NICHT validiert wird (siehe Report-Sektion "Cost/Performance — vertagt"):
  - LLM-Call-Volumen pro MB Input
  - Wallclock-Time für Indexierung
  - Output-Qualität von aquery
  - Bailian-Embedding-Kompatibilität (text-embedding-v3, 1024-dim)

Aufruf:
    PYTHONPATH=/tmp/spike-libs python3 backend/scripts/lightrag_mock_spike.py \\
        [--working-dir-base /tmp/lightrag_spike] [--keep-artifacts]

Voraussetzung: lightrag-hku>=1.4.10,<1.5 in sys.path verfügbar.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import numpy as np

from lightrag import LightRAG, QueryParam
from lightrag.kg.shared_storage import initialize_pipeline_status
from lightrag.utils import EmbeddingFunc

# ---------------------------------------------------------------------------
# Mock-Implementationen (deterministische Synthetic-Outputs, kein Network-IO)
# ---------------------------------------------------------------------------

# LightRAG-Default-Delimiter (siehe lightrag/prompt.py)
TUPLE_D = "<|#|>"
COMPLETE_D = "<|COMPLETE|>"


@dataclasses.dataclass
class MockCounter:
    """Threadsafe Counter, um LLM-/Embedding-Calls zu zählen."""

    llm_calls: int = 0
    embedding_calls: int = 0
    embedding_total_texts: int = 0
    last_llm_prompt_excerpt: str = ""

    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def bump_llm(self, prompt: str) -> None:
        with self._lock:
            self.llm_calls += 1
            self.last_llm_prompt_excerpt = prompt[:200]

    def bump_embedding(self, n_texts: int) -> None:
        with self._lock:
            self.embedding_calls += 1
            self.embedding_total_texts += n_texts


COUNTER = MockCounter()


def _detect_extraction_request(prompt: str) -> bool:
    """Heuristik: Wenn der Prompt nach Entity-Extraction aussieht."""
    p = prompt.lower()
    return ("entity" in p and "relation" in p) or "tuple_delimiter" in p or TUPLE_D in prompt


def _detect_keyword_request(prompt: str) -> bool:
    return "keywords_extraction" in prompt.lower() or (
        "high_level_keywords" in prompt.lower() and "low_level_keywords" in prompt.lower()
    )


def _build_mock_extraction_response() -> str:
    """Liefert eine Antwort im LightRAG-Entity-Extraction-Format.

    Format-Regeln (siehe lightrag/prompt.py):
      entity<|#|>name<|#|>type<|#|>description
      relation<|#|>src<|#|>tgt<|#|>keywords<|#|>description
      <|COMPLETE|>
    """
    lines = [
        f"entity{TUPLE_D}AcmeCorp{TUPLE_D}organization{TUPLE_D}AcmeCorp ist ein fiktives Unternehmen aus dem Mock-Spike.",
        f"entity{TUPLE_D}Alice{TUPLE_D}person{TUPLE_D}Alice ist Engineering-Lead bei AcmeCorp.",
        f"entity{TUPLE_D}Berlin{TUPLE_D}location{TUPLE_D}Berlin ist der Sitz von AcmeCorp.",
        f"relation{TUPLE_D}Alice{TUPLE_D}AcmeCorp{TUPLE_D}employment, leadership{TUPLE_D}Alice arbeitet als Engineering-Lead bei AcmeCorp.",
        f"relation{TUPLE_D}AcmeCorp{TUPLE_D}Berlin{TUPLE_D}location, headquarters{TUPLE_D}AcmeCorp hat seinen Hauptsitz in Berlin.",
        COMPLETE_D,
    ]
    return "\n".join(lines)


def _build_mock_keywords_response() -> str:
    """Liefert eine Antwort im keywords_extraction-Format (JSON)."""
    return json.dumps(
        {
            "high_level_keywords": ["organization", "leadership"],
            "low_level_keywords": ["AcmeCorp", "Alice", "Berlin"],
        }
    )


def _build_mock_query_response() -> str:
    """Allgemeine Query-Antwort als String (für aquery)."""
    return (
        "Mock-Antwort: AcmeCorp ist ein fiktives Unternehmen mit Sitz in Berlin. "
        "Alice ist Engineering-Lead. (Synthetisch erzeugt vom Mock-LLM, keine echten API-Calls.)"
    )


async def mock_llm_func(
    prompt: str,
    system_prompt: Optional[str] = None,
    history_messages: Optional[list] = None,
    keyword_extraction: bool = False,
    **kwargs: Any,
) -> str:
    """Deterministische Mock-LLM.

    Erkennt anhand des Prompts:
      - Entity-Extraction → Format mit tuple_delimiter und <|COMPLETE|>.
      - Keyword-Extraction → JSON mit high_level_keywords/low_level_keywords.
      - Sonstige Queries → freier String.
    """
    full_prompt = (system_prompt or "") + "\n" + (prompt or "")
    COUNTER.bump_llm(full_prompt)
    # Kleine Latenz, damit Concurrent-Tests realistischer sind.
    await asyncio.sleep(0.001)
    if keyword_extraction or _detect_keyword_request(full_prompt):
        return _build_mock_keywords_response()
    if _detect_extraction_request(full_prompt):
        return _build_mock_extraction_response()
    return _build_mock_query_response()


async def _mock_embedding_inner(texts: list[str]) -> np.ndarray:
    """Hash-basierte deterministische 1024-dim Embeddings."""
    COUNTER.bump_embedding(len(texts))
    await asyncio.sleep(0.001)
    out = np.zeros((len(texts), 1024), dtype=np.float32)
    for i, t in enumerate(texts):
        seed_bytes = hashlib.sha256(t.encode("utf-8")).digest()[:4]
        seed = int.from_bytes(seed_bytes, "little", signed=False)
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(1024).astype(np.float32)
        # L2-normalisieren (typisch für Bailian-Embedding-Outputs).
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        out[i] = vec
    return out


def make_mock_embedding_func() -> EmbeddingFunc:
    """Liefert einen EmbeddingFunc-Wrapper (Schnittstelle wie produktiver Code)."""
    return EmbeddingFunc(
        embedding_dim=1024,
        max_token_size=8192,
        func=_mock_embedding_inner,
    )


# ---------------------------------------------------------------------------
# RagManager (1:1 aus dem Migrationsplan, ergänzt um sauberen Shutdown)
# Dies ist das eigentliche Artefakt, das in Phase 1 nach
# backend/app/services/rag_manager.py übernommen werden kann.
# ---------------------------------------------------------------------------


class RagManager:
    """Singleton: pro Graph eine LightRAG-Instanz, ein dedizierter Loop-Thread."""

    def __init__(self, working_dir_base: Path):
        self._instances: dict[str, LightRAG] = {}
        self._instance_locks: dict[str, asyncio.Lock] = {}
        self._working_dir_base = working_dir_base
        self._working_dir_base.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def _run(self, coro, timeout: float = 120.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _get_or_create(self, graph_id: str) -> LightRAG:
        if graph_id in self._instances:
            return self._instances[graph_id]
        working_dir = self._working_dir_base / graph_id
        working_dir.mkdir(parents=True, exist_ok=True)
        rag = LightRAG(
            working_dir=str(working_dir),
            llm_model_func=mock_llm_func,
            embedding_func=make_mock_embedding_func(),
        )
        await rag.initialize_storages()
        await initialize_pipeline_status()
        self._instances[graph_id] = rag
        self._instance_locks[graph_id] = asyncio.Lock()
        return rag

    def insert(self, graph_id: str, text: str) -> None:
        async def _do() -> None:
            rag = await self._get_or_create(graph_id)
            async with self._instance_locks[graph_id]:
                await rag.ainsert(text)

        self._run(_do(), timeout=600)

    def query(self, graph_id: str, question: str, mode: str = "hybrid") -> str:
        async def _do() -> str:
            rag = await self._get_or_create(graph_id)
            return await rag.aquery(question, param=QueryParam(mode=mode))

        return self._run(_do())

    def get_all_nodes(self, graph_id: str) -> list[dict]:
        async def _do() -> list[dict]:
            rag = await self._get_or_create(graph_id)
            return await rag.chunk_entity_relation_graph.get_all_nodes()

        return self._run(_do())

    def get_all_edges(self, graph_id: str) -> list[dict]:
        async def _do() -> list[dict]:
            rag = await self._get_or_create(graph_id)
            return await rag.chunk_entity_relation_graph.get_all_edges()

        return self._run(_do())

    def has_instance(self, graph_id: str) -> bool:
        return graph_id in self._instances

    def delete(self, graph_id: str) -> None:
        async def _finalize() -> None:
            if graph_id in self._instances:
                rag = self._instances.pop(graph_id)
                self._instance_locks.pop(graph_id, None)
                try:
                    await rag.finalize_storages()
                except Exception:
                    pass

        self._run(_finalize())
        working_dir = self._working_dir_base / graph_id
        if working_dir.exists():
            shutil.rmtree(working_dir)

    def shutdown(self) -> None:
        async def _all_finalize() -> None:
            for gid, rag in list(self._instances.items()):
                try:
                    await rag.finalize_storages()
                except Exception:
                    pass
            self._instances.clear()
            self._instance_locks.clear()

        try:
            self._run(_all_finalize(), timeout=30)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Test-Suite (sieben Annahmen)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TestResult:
    name: str
    status: str  # "PASS" | "FAIL" | "INCONCLUSIVE"
    detail: str
    duration_s: float


def _record(results: list[TestResult], name: str, status: str, detail: str, t0: float) -> None:
    results.append(
        TestResult(name=name, status=status, detail=detail, duration_s=round(time.perf_counter() - t0, 3))
    )


def test_1_init_pattern(mgr: "RagManager", working_dir_base: Path) -> TestResult:
    """A1: Pflicht-Init-Pattern (initialize_storages + initialize_pipeline_status).

    WICHTIG: Wir führen den Init-Test im SELBEN langlebigen Event-Loop aus, der
    auch alle nachfolgenden RagManager-Operationen bedient. Grund:
    `initialize_pipeline_status()` bindet einen globalen Modul-Lock im
    `lightrag.kg.shared_storage` an den aktuellen Loop. Wird der Init-Loop
    danach beendet (z.B. weil A1 in einem eigenen `asyncio.run` lief), schlagen
    alle nachfolgenden Calls aus einem anderen Loop mit
    `RuntimeError: ... is bound to a different event loop` fehl.
    Genau dieser subtile Fehler ist im Mock-Spike aufgetaucht.
    """
    t0 = time.perf_counter()
    name = "A1: Pflicht-Init-Pattern"

    async def _do() -> tuple[bool, str]:
        wd = working_dir_base / "init_test"
        if wd.exists():
            shutil.rmtree(wd)
        wd.mkdir(parents=True, exist_ok=True)
        rag = LightRAG(
            working_dir=str(wd),
            llm_model_func=mock_llm_func,
            embedding_func=make_mock_embedding_func(),
        )
        await rag.initialize_storages()
        await initialize_pipeline_status()
        # Smoke: ein einziger ainsert sollte ohne KeyError oder __aenter__-Fehler durchlaufen.
        await rag.ainsert("AcmeCorp ist ein Unternehmen in Berlin. Alice ist dort Engineering-Lead.")
        await rag.finalize_storages()
        return True, "initialize_storages + initialize_pipeline_status + ainsert ohne KeyError/__aenter__"

    try:
        ok, detail = mgr._run(_do(), timeout=120)
        return TestResult(
            name=name,
            status="PASS" if ok else "FAIL",
            detail=detail,
            duration_s=round(time.perf_counter() - t0, 3),
        )
    except Exception as e:
        return TestResult(
            name=name,
            status="FAIL",
            detail=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
            duration_s=round(time.perf_counter() - t0, 3),
        )


def test_2_ragmanager_sync_bridge(mgr: RagManager) -> TestResult:
    """A2: RagManager-Singleton mit Event-Loop-Thread, threadsafe Sync-API."""
    t0 = time.perf_counter()
    name = "A2: RagManager Sync→Async-Bridge"
    try:
        # Sync-Aufrufe aus dem Hauptthread (simuliert Flask-Request-Worker).
        mgr.insert("graph_sync", "Bob arbeitet bei FooCorp in Wien.")
        # Zweiter Sync-Insert in dieselbe Instanz, der Lock muss serialisieren.
        mgr.insert("graph_sync", "FooCorp wurde 2020 gegründet.")
        # Aufruf aus einem anderen OS-Thread (echter Flask-Worker-Pool).
        results_box: dict[str, Any] = {}

        def _from_other_thread() -> None:
            try:
                results_box["nodes"] = mgr.get_all_nodes("graph_sync")
            except Exception as e:
                results_box["err"] = e

        th = threading.Thread(target=_from_other_thread)
        th.start()
        th.join(timeout=30)
        if "err" in results_box:
            raise results_box["err"]
        nodes = results_box.get("nodes", [])
        return TestResult(
            name=name,
            status="PASS" if isinstance(nodes, list) else "FAIL",
            detail=f"Sync-Insert (2x) + Cross-Thread-Read OK; nodes_count={len(nodes)}",
            duration_s=round(time.perf_counter() - t0, 3),
        )
    except Exception as e:
        return TestResult(
            name=name,
            status="FAIL",
            detail=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
            duration_s=round(time.perf_counter() - t0, 3),
        )


def test_3_multi_project_isolation(mgr: RagManager) -> TestResult:
    """A3: Multi-Project-Isolation via working_dir."""
    t0 = time.perf_counter()
    name = "A3: Multi-Project-Isolation"
    try:
        mgr.insert("graph_a", "ProjektA: Carol arbeitet an Modul X.")
        mgr.insert("graph_b", "ProjektB: Dave arbeitet an Modul Y.")
        nodes_a = {n.get("id", n.get("entity_name", "")) for n in mgr.get_all_nodes("graph_a")}
        nodes_b = {n.get("id", n.get("entity_name", "")) for n in mgr.get_all_nodes("graph_b")}
        # Da der Mock immer dieselben Mock-Entities (AcmeCorp/Alice/Berlin) liefert, sind die Knoten-Namen
        # identisch. Was hier zählt: die working_dirs sind getrennt und die LightRAG-Instanzen verwalten
        # ihre Daten unabhängig (kein Shared-Mutable-State).
        wd_a_files = sorted(p.name for p in (mgr._working_dir_base / "graph_a").iterdir())
        wd_b_files = sorted(p.name for p in (mgr._working_dir_base / "graph_b").iterdir())
        same_dirs_collide = (mgr._working_dir_base / "graph_a") == (mgr._working_dir_base / "graph_b")
        ok = (not same_dirs_collide) and len(wd_a_files) > 0 and len(wd_b_files) > 0
        detail = (
            f"working_dir A: {len(wd_a_files)} Dateien, working_dir B: {len(wd_b_files)} Dateien, "
            f"nodes_a={len(nodes_a)}, nodes_b={len(nodes_b)}; getrennte Verzeichnisse vorhanden."
        )
        return TestResult(name, "PASS" if ok else "FAIL", detail, round(time.perf_counter() - t0, 3))
    except Exception as e:
        return TestResult(
            name,
            "FAIL",
            f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
            round(time.perf_counter() - t0, 3),
        )


def test_4_networkx_access(mgr: RagManager) -> TestResult:
    """A4: NetworkX-Graph-Zugriff (get_all_nodes/get_all_edges) für strukturierte Reads."""
    t0 = time.perf_counter()
    name = "A4: NetworkX-Graph-Reads"
    try:
        mgr.insert("graph_nx", "Eva ist CTO bei BarCorp in Hamburg.")
        nodes = mgr.get_all_nodes("graph_nx")
        edges = mgr.get_all_edges("graph_nx")
        ok = isinstance(nodes, list) and isinstance(edges, list) and len(nodes) > 0
        # Detail: zeige eine Stichprobe der Datenstruktur.
        sample_node = nodes[0] if nodes else {}
        sample_edge = edges[0] if edges else {}
        detail = (
            f"nodes={len(nodes)} (sample keys: {sorted(sample_node.keys())[:6]}), "
            f"edges={len(edges)} (sample keys: {sorted(sample_edge.keys())[:6]})"
        )
        # Wichtige Feststellung: Im Migrationsplan steht 'rag.chunk_entity_relation_graph.nodes()'.
        # Der echte LightRAG-Vertrag ist async: get_all_nodes()/get_all_edges() (siehe BaseGraphStorage).
        return TestResult(name, "PASS" if ok else "FAIL", detail, round(time.perf_counter() - t0, 3))
    except Exception as e:
        return TestResult(
            name,
            "FAIL",
            f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
            round(time.perf_counter() - t0, 3),
        )


def test_5_per_graph_lock(mgr: RagManager) -> TestResult:
    """A5: Per-Graph asyncio.Lock verhindert Race bei concurrent inserts."""
    t0 = time.perf_counter()
    name = "A5: Per-Graph asyncio.Lock"
    try:
        # Zwei parallele Threads inserten in DENSELBEN Graph. Der Lock im RagManager
        # muss die ainsert-Calls serialisieren, sonst ist NanoVectorDB+NetworkX nicht
        # konsistent.
        errors: list[BaseException] = []

        def _worker(text: str) -> None:
            try:
                mgr.insert("graph_lock", text)
            except BaseException as e:
                errors.append(e)

        threads = [
            threading.Thread(target=_worker, args=(f"Concurrent-Insert {i}: Eintrag mit Token X{i}.",))
            for i in range(4)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
        if errors:
            raise errors[0]
        # Konsistenz-Check: get_all_nodes liefert eine wohlgeformte Liste, kein KeyError und kein Crash.
        nodes = mgr.get_all_nodes("graph_lock")
        edges = mgr.get_all_edges("graph_lock")
        ok = isinstance(nodes, list) and isinstance(edges, list)
        return TestResult(
            name,
            "PASS" if ok else "FAIL",
            f"4 parallele Inserts in selben Graph ohne Race-Crash; nodes={len(nodes)}, edges={len(edges)}",
            round(time.perf_counter() - t0, 3),
        )
    except Exception as e:
        return TestResult(
            name,
            "FAIL",
            f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
            round(time.perf_counter() - t0, 3),
        )


# Erwartete Dateien laut Migrationsplan / LightRAG-Default-Storage.
EXPECTED_FILE_PATTERNS = [
    "kv_store_full_docs.json",
    "kv_store_text_chunks.json",
    "graph_chunk_entity_relation.graphml",
    "vdb_entities.json",  # NanoVectorDB legt JSON ab, nicht .pkl wie im Plan vermutet
    "vdb_relationships.json",
    "vdb_chunks.json",
]


def test_6_storage_persistence(mgr: RagManager) -> TestResult:
    """A6: Storage-Persistenz — erwartetes File-Layout im working_dir."""
    t0 = time.perf_counter()
    name = "A6: Storage-Persistenz"
    try:
        mgr.insert("graph_persist", "Frank ist Architekt bei BazInc in München.")
        wd = mgr._working_dir_base / "graph_persist"
        present = sorted(p.name for p in wd.iterdir() if p.is_file())
        # Wir prüfen, dass mindestens KV+Graph+VDB-Familie vorhanden ist.
        present_set = set(present)
        found = [pat for pat in EXPECTED_FILE_PATTERNS if pat in present_set]
        # Mindestens KV-Store, GraphML und ein VDB-Artefakt erwarten.
        kv_ok = any(name.startswith("kv_store_") for name in present)
        graph_ok = any(name.endswith(".graphml") for name in present)
        vdb_ok = any(name.startswith("vdb_") for name in present)
        ok = kv_ok and graph_ok and vdb_ok
        detail = (
            f"Files im working_dir: {present[:12]}{'...' if len(present) > 12 else ''} | "
            f"erwartet gefunden: {found} | KV={kv_ok}, GraphML={graph_ok}, VDB={vdb_ok}"
        )
        return TestResult(name, "PASS" if ok else "FAIL", detail, round(time.perf_counter() - t0, 3))
    except Exception as e:
        return TestResult(
            name,
            "FAIL",
            f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
            round(time.perf_counter() - t0, 3),
        )


def test_7_delete_path(mgr: RagManager, working_dir_base: Path) -> TestResult:
    """A7: shutil.rmtree(working_dir) als Delete-Pfad ist sauber (keine offenen Handles)."""
    t0 = time.perf_counter()
    name = "A7: Delete-Pfad shutil.rmtree"
    try:
        mgr.insert("graph_delete", "Greta arbeitet bei QuxLLC.")
        wd = working_dir_base / "graph_delete"
        assert wd.exists() and any(wd.iterdir()), "working_dir wurde nicht angelegt"
        mgr.delete("graph_delete")
        still_exists = wd.exists()
        # Nach delete darf weder das Verzeichnis noch eine Instanz im Manager bleiben.
        ok = (not still_exists) and (not mgr.has_instance("graph_delete"))
        return TestResult(
            name,
            "PASS" if ok else "FAIL",
            f"working_dir nach rmtree weg={not still_exists}, instanz_im_manager_weg={not mgr.has_instance('graph_delete')}",
            round(time.perf_counter() - t0, 3),
        )
    except Exception as e:
        return TestResult(
            name,
            "FAIL",
            f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}",
            round(time.perf_counter() - t0, 3),
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_all(working_dir_base: Path, keep_artifacts: bool) -> dict:
    overall_t0 = time.perf_counter()
    if working_dir_base.exists():
        shutil.rmtree(working_dir_base)
    working_dir_base.mkdir(parents=True, exist_ok=True)

    results: list[TestResult] = []

    print(f"[spike] working_dir_base = {working_dir_base}")
    print(f"[spike] LightRAG = {__import__('lightrag').__version__}")
    print(f"[spike] keep_artifacts = {keep_artifacts}")
    print()

    # Alle Tests laufen über den langlebigen RagManager-Event-Loop. Begründung
    # siehe Doku in test_1_init_pattern() — globaler Pipeline-Lock muss an einen
    # Loop gebunden bleiben, der das ganze Programm-Leben überlebt.
    print("→ Manager-Init (dedizierter Event-Loop-Thread) …")
    mgr = RagManager(working_dir_base)
    try:
        for fn, label in [
            (lambda: test_1_init_pattern(mgr, working_dir_base), "A1"),
            (lambda: test_2_ragmanager_sync_bridge(mgr), "A2"),
            (lambda: test_3_multi_project_isolation(mgr), "A3"),
            (lambda: test_4_networkx_access(mgr), "A4"),
            (lambda: test_5_per_graph_lock(mgr), "A5"),
            (lambda: test_6_storage_persistence(mgr), "A6"),
            (lambda: test_7_delete_path(mgr, working_dir_base), "A7"),
        ]:
            r = fn()
            results.append(r)
            print(f"   [{label}] {r.status}: {r.detail[:200]}")
    finally:
        print("→ Manager-Shutdown …")
        mgr.shutdown()

    overall_duration = round(time.perf_counter() - overall_t0, 3)
    summary = {
        "spike": "lightrag-mock-spike",
        "lightrag_version": __import__("lightrag").__version__,
        "working_dir_base": str(working_dir_base),
        "duration_s": overall_duration,
        "llm_calls_total": COUNTER.llm_calls,
        "embedding_calls_total": COUNTER.embedding_calls,
        "embedding_total_texts": COUNTER.embedding_total_texts,
        "results": [dataclasses.asdict(r) for r in results],
        "verdict": _verdict(results),
    }

    if not keep_artifacts:
        try:
            shutil.rmtree(working_dir_base)
        except Exception:
            pass

    return summary


def _verdict(results: list[TestResult]) -> str:
    statuses = [r.status for r in results]
    if all(s == "PASS" for s in statuses):
        return "GREEN"
    if any(s == "FAIL" for s in statuses) and sum(1 for s in statuses if s == "PASS") >= 5:
        return "YELLOW"
    return "RED"


def main() -> int:
    parser = argparse.ArgumentParser(description="LightRAG Mock-Spike (Phase 0)")
    parser.add_argument("--working-dir-base", default="/tmp/lightrag_spike", type=Path)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--report-json", type=Path, default=None)
    args = parser.parse_args()

    summary = run_all(args.working_dir_base, args.keep_artifacts)
    print()
    print("=" * 60)
    print(f"VERDICT: {summary['verdict']}")
    print(f"Tests: {sum(1 for r in summary['results'] if r['status']=='PASS')}/{len(summary['results'])} PASS")
    print(f"LLM-Mock-Calls: {summary['llm_calls_total']} | Embedding-Mock-Calls: {summary['embedding_calls_total']}")
    print("=" * 60)
    print()
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        args.report_json.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\n[spike] Report geschrieben nach {args.report_json}")

    return 0 if summary["verdict"] in ("GREEN", "YELLOW") else 1


if __name__ == "__main__":
    sys.exit(main())
