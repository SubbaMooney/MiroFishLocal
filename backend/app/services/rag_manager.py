"""
RagManager — Singleton-Verwalter pro-Graph LightRAG-Instanzen.

Phase 1 der Zep->LightRAG-Migration (siehe docs/MIGRATION-ZEP-TO-LIGHTRAG.md).
1:1-Port aus dem validierten Mock-Spike (backend/scripts/lightrag_mock_spike.py)
mit Anbindung an die Produktiv-Factory.

Architektur (im Mock-Spike validiert):
  - Ein langlebiger Event-Loop in einem dedizierten Daemon-Thread.
  - Pro Graph eine eigene LightRAG-Instanz (eigener working_dir).
  - Pro Graph ein asyncio.Lock, der konkurrente Inserts auf derselben Instanz
    serialisiert (verhindert NanoVectorDB-Race).
  - Sync->async-Bridge ueber asyncio.run_coroutine_threadsafe — Caller bleibt
    synchron, der Loop laeuft im Hintergrund weiter.

Pflicht-Invariante (siehe lightrag_factory.create_rag):
  initialize_pipeline_status bindet einen prozessweiten Lock an den aktuellen
  Event-Loop. ALLE LightRAG-Operationen MUESSEN ueber denselben Loop laufen,
  sonst fliegen Folge-Calls mit "bound to a different event loop" raus.
  Diese Klasse erzwingt das, indem nur _run() Coroutines an den Loop
  schickt — kein direktes asyncio.run anderswo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

from ..config import Config
from . import lightrag_factory

logger = logging.getLogger(__name__)

# Default-Timeout fuer kurze Reads/Queries; Inserts brauchen mehr Zeit.
_DEFAULT_TIMEOUT_S = 120.0
_INSERT_TIMEOUT_S = 600.0

_ONTOLOGY_FILE = "ontology.json"


def _format_ontology_hint(ontology: dict) -> str:
    """Konvertiert eine Ontology-Definition in einen kompakten System-Prompt-Hint.

    LightRAG kennt kein Schema, daher injizieren wir die Ontologie als
    natuerlich-sprachlichen Hint, den der Extraktor beruecksichtigen soll.
    Bewusst kompakt gehalten — der Hint geht in JEDEN LLM-Call dieser Instanz.
    """
    entity_lines: list[str] = []
    for et in ontology.get("entity_types", []):
        name = et.get("name", "")
        desc = et.get("description", "")
        if name:
            entity_lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")

    edge_lines: list[str] = []
    for ed in ontology.get("edge_types", []):
        name = ed.get("name", "")
        desc = ed.get("description", "")
        targets = ed.get("source_targets", [])
        target_str = ""
        if targets:
            pairs = [f"{t.get('source', '?')}->{t.get('target', '?')}" for t in targets]
            target_str = f" ({', '.join(pairs)})"
        if name:
            edge_lines.append(
                f"  - {name}{target_str}: {desc}" if desc else f"  - {name}{target_str}"
            )

    parts: list[str] = ["Knowledge Graph Schema Hint (prefer these when extracting):"]
    if entity_lines:
        parts.append("Entity types:")
        parts.extend(entity_lines)
    if edge_lines:
        parts.append("Relation types:")
        parts.extend(edge_lines)
    return "\n".join(parts)


class RagManager:
    """Singleton: pro graph_id eine LightRAG-Instanz, ein dedizierter Loop-Thread.

    Lifecycle:
      mgr = RagManager()           # Loop-Thread startet
      mgr.insert(graph_id, text)   # Sync-Call, blockiert bis insert fertig
      mgr.query(graph_id, q)       # Sync-Call, liefert String
      mgr.delete(graph_id)         # Finalize + rmtree(working_dir)
      mgr.shutdown()               # Alle Instanzen finalize, Loop stoppen
    """

    _instance: Optional["RagManager"] = None
    _instance_lock = threading.Lock()

    def __init__(self, working_dir_base: Optional[Path] = None):
        # Working-Dir: Default aus Config, ueberschreibbar fuer Tests.
        base = working_dir_base or Path(Config.LIGHTRAG_WORKING_DIR_BASE)
        self._working_dir_base = Path(base).resolve()
        self._working_dir_base.mkdir(parents=True, exist_ok=True)

        self._instances: dict[str, Any] = {}  # graph_id -> LightRAG
        self._instance_locks: dict[str, asyncio.Lock] = {}
        # Pro-Graph Ontology-Hints (mutable; werden vom hint_provider live gelesen).
        self._ontology_hints: dict[str, str] = {}

        # Langlebiger Loop in eigenem Daemon-Thread (siehe Pflicht-Invariante).
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="rag-manager-loop",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "RagManager initialisiert: working_dir_base=%s, thread=%s",
            self._working_dir_base, self._thread.name,
        )

    # ------------------------------------------------------------------
    # Singleton-Zugriff
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "RagManager":
        """Liefert die prozessweite Singleton-Instanz (lazy)."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """Nur fuer Tests — schliesst die alte Instanz und vergisst sie."""
        with cls._instance_lock:
            if cls._instance is not None:
                try:
                    cls._instance.shutdown()
                except Exception:  # pragma: no cover
                    logger.exception("Fehler beim Singleton-Shutdown")
                cls._instance = None

    # ------------------------------------------------------------------
    # Sync->async-Bridge (immer ueber den eigenen Loop)
    # ------------------------------------------------------------------

    def _run(self, coro, timeout: float = _DEFAULT_TIMEOUT_S):
        """Sync-Wrapper: Coroutine auf dem Singleton-Loop ausfuehren und
        Ergebnis blockierend zurueckliefern."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------
    # Per-Graph Instanz-Verwaltung
    # ------------------------------------------------------------------

    async def _get_or_create(self, graph_id: str):
        """Holt oder erzeugt eine LightRAG-Instanz fuer graph_id.

        Muss innerhalb des Manager-Loops aufgerufen werden — verlaesst sich
        auf asyncio.get_event_loop() in den Factory-Funktionen.
        """
        if graph_id in self._instances:
            return self._instances[graph_id]

        working_dir = self._working_dir_base / graph_id
        working_dir.mkdir(parents=True, exist_ok=True)

        # Persistierte Ontologie aus vorherigem Run wiederherstellen, falls vorhanden.
        ontology_path = working_dir / _ONTOLOGY_FILE
        if graph_id not in self._ontology_hints and ontology_path.exists():
            try:
                ontology = json.loads(ontology_path.read_text(encoding="utf-8"))
                self._ontology_hints[graph_id] = _format_ontology_hint(ontology)
            except Exception:
                logger.exception("Ontology-Restore fehlgeschlagen: %s", ontology_path)

        rag = await lightrag_factory.create_rag(
            str(working_dir),
            system_prompt_hint_provider=lambda: self._ontology_hints.get(graph_id, ""),
        )
        self._instances[graph_id] = rag
        self._instance_locks[graph_id] = asyncio.Lock()
        logger.info("LightRAG-Instanz erzeugt: graph_id=%s, dir=%s", graph_id, working_dir)
        return rag

    def has_instance(self, graph_id: str) -> bool:
        return graph_id in self._instances

    # ------------------------------------------------------------------
    # Public Sync-API
    # ------------------------------------------------------------------

    def set_ontology(self, graph_id: str, ontology: dict) -> None:
        """Registriert eine Ontologie als System-Prompt-Hint fuer diesen Graph.

        Persistiert die Ontologie zusaetzlich als ``ontology.json`` im
        working_dir, damit sie nach Prozess-Restart automatisch wiederhergestellt
        wird (siehe ``_get_or_create``).

        Kann VOR oder NACH dem ersten ``insert`` aufgerufen werden — der
        hint_provider liest live aus ``self._ontology_hints``, daher greift
        eine spaetere Aenderung sofort beim naechsten LLM-Call.
        """
        hint = _format_ontology_hint(ontology)
        self._ontology_hints[graph_id] = hint

        working_dir = self._working_dir_base / graph_id
        working_dir.mkdir(parents=True, exist_ok=True)
        (working_dir / _ONTOLOGY_FILE).write_text(
            json.dumps(ontology, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Ontology gesetzt: graph_id=%s, %d Entity-Types, %d Edge-Types",
                    graph_id,
                    len(ontology.get("entity_types", [])),
                    len(ontology.get("edge_types", [])))

    def insert(self, graph_id: str, text: str) -> None:
        """Inserts text in den Graph (synchron, blockiert bis fertig)."""
        async def _do() -> None:
            rag = await self._get_or_create(graph_id)
            async with self._instance_locks[graph_id]:
                await rag.ainsert(text)
        self._run(_do(), timeout=_INSERT_TIMEOUT_S)

    def query(self, graph_id: str, question: str, mode: str = "hybrid") -> str:
        """Stellt eine Frage und liefert die LLM-generierte Antwort als String.

        Modes (siehe LightRAG-Doku):
          local  — direkte Entity-Suche
          global — themenbasiert, breit
          hybrid — Kombination
          mix    — kombiniert mit Vector-Search
          naive  — reiner Vector-Search ohne Graph
        """
        # Lazy-Import nur bei Bedarf — hilft, Tests ohne lightrag laufen zu lassen.
        from lightrag import QueryParam

        async def _do() -> str:
            rag = await self._get_or_create(graph_id)
            return await rag.aquery(question, param=QueryParam(mode=mode))
        return self._run(_do())

    def get_all_nodes(self, graph_id: str) -> list[dict]:
        """NetworkX-Knoten direkt extrahieren (strukturiert, kein LLM-Call)."""
        async def _do() -> list[dict]:
            rag = await self._get_or_create(graph_id)
            return await rag.chunk_entity_relation_graph.get_all_nodes()
        return self._run(_do())

    def get_all_edges(self, graph_id: str) -> list[dict]:
        """NetworkX-Edges direkt extrahieren (strukturiert, kein LLM-Call)."""
        async def _do() -> list[dict]:
            rag = await self._get_or_create(graph_id)
            return await rag.chunk_entity_relation_graph.get_all_edges()
        return self._run(_do())

    def delete(self, graph_id: str) -> None:
        """Entfernt eine Graph-Instanz vollstaendig (Storages + working_dir)."""
        async def _finalize() -> None:
            if graph_id in self._instances:
                rag = self._instances.pop(graph_id)
                self._instance_locks.pop(graph_id, None)
                try:
                    await rag.finalize_storages()
                except Exception:  # pragma: no cover
                    logger.exception("finalize_storages fehlgeschlagen: %s", graph_id)
        self._run(_finalize())
        self._ontology_hints.pop(graph_id, None)

        working_dir = self._working_dir_base / graph_id
        if working_dir.exists():
            shutil.rmtree(working_dir)
            logger.info("Graph geloescht: graph_id=%s", graph_id)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Schliesst alle Instanzen sauber und stoppt den Loop-Thread.

        Ist idempotent — kann mehrfach aufgerufen werden (z.B. via atexit).
        """
        if not self._loop.is_running():
            return

        async def _all_finalize() -> None:
            for gid, rag in list(self._instances.items()):
                try:
                    await rag.finalize_storages()
                except Exception:  # pragma: no cover
                    logger.exception("finalize_storages fehlgeschlagen: %s", gid)
            self._instances.clear()
            self._instance_locks.clear()

        try:
            self._run(_all_finalize(), timeout=30)
        except Exception:  # pragma: no cover
            logger.exception("Sammel-Finalize fehlgeschlagen")

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        logger.info("RagManager heruntergefahren")


__all__ = ["RagManager"]
