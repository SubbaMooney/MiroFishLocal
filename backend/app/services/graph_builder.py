"""
图谱构建服务

Phase 2 Migration (siehe docs/MIGRATION-ZEP-TO-LIGHTRAG.md):
Ersetzt den Zep-Cloud-Backend-Pfad durch LightRAG (lokal, NetworkX-basiert).

Architektur-Wechsel im Vergleich zum Zep-Pfad:
  - ``create_graph`` legt nur noch eine lokale ID an. Die LightRAG-Instanz
    wird lazy beim ersten Insert vom ``RagManager`` erzeugt.
  - ``set_ontology`` schreibt die Ontologie als ``ontology.json`` in den
    working_dir und registriert sie als System-Prompt-Hint (die Extraktion
    folgt LightRAG-eigener Logik, kein hartes Schema mehr).
  - ``add_text_batches`` ist jetzt SYNCHRON — ``RagManager.insert`` blockiert
    bis NetworkX-Storage fertig ist. Damit entfaellt das ``_wait_for_episodes``
    Polling vollstaendig.
  - ``get_graph_data`` baut die Frontend-API aus ``RagManager.get_all_nodes/
    get_all_edges`` zusammen — strukturierte Reads ohne LLM-Call.
  - ``delete_graph`` ruft ``RagManager.delete`` (finalize + rmtree).

Episode-UUIDs gibt es nicht mehr; ``add_text_batches`` liefert eine leere
Liste zurueck (Signatur fuer Backward-Compat im Worker-Code beibehalten).
"""

import uuid
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..models.task import TaskManager, TaskStatus
from ..utils.locale import get_locale, set_locale, t
from ._networkx_mapping import _edge_get, _node_get
from .rag_manager import RagManager
from .text_processor import TextProcessor


@dataclass
class GraphInfo:
    """图谱信息"""
    graph_id: str
    node_count: int
    edge_count: int
    entity_types: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entity_types": self.entity_types,
        }


class GraphBuilderService:
    """
    图谱构建服务
    LightRAG-basiert (Phase 2 Migration). Kapselt ``RagManager`` fuer den
    Indexing-Pfad — Caller-API ist soweit wie moeglich kompatibel zur
    vorherigen Zep-Implementierung.
    """

    def __init__(self):
        self.task_manager = TaskManager()
        self.rag = RagManager.get_instance()

    def build_graph_async(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str = "MiroFish Graph",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 3,
    ) -> str:
        """
        异步构建图谱

        Args:
            text: 输入文本
            ontology: 本体定义（来自接口1的输出）
            graph_name: 图谱名称
            chunk_size: 文本块大小
            chunk_overlap: 块重叠大小
            batch_size: 进度回调粒度（LightRAG insertet pro Chunk synchron）

        Returns:
            任务ID
        """
        task_id = self.task_manager.create_task(
            task_type="graph_build",
            metadata={
                "graph_name": graph_name,
                "chunk_size": chunk_size,
                "text_length": len(text),
            },
        )

        current_locale = get_locale()

        thread = threading.Thread(
            target=self._build_graph_worker,
            args=(task_id, text, ontology, graph_name, chunk_size, chunk_overlap, batch_size, current_locale),
        )
        thread.daemon = True
        thread.start()

        return task_id

    def _build_graph_worker(
        self,
        task_id: str,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int,
        locale: str = "zh",
    ):
        """图谱构建工作线程

        Phase-2-Pipeline (Polling-Phase entfaellt):
          5%  Start
          10% Graph-ID erzeugt
          15% Ontology gesetzt (System-Prompt-Hint registriert)
          20% Text gechunkt
          20-85% LightRAG-Insert pro Chunk (synchron, blockt bis NetworkX persistiert)
          90% Graph-Info aus NetworkX gelesen
          100% Fertig
        """
        set_locale(locale)
        try:
            self.task_manager.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                progress=5,
                message=t("progress.startBuildingGraph"),
            )

            graph_id = self.create_graph(graph_name)
            self.task_manager.update_task(
                task_id,
                progress=10,
                message=t("progress.graphCreated", graphId=graph_id),
            )

            self.set_ontology(graph_id, ontology)
            self.task_manager.update_task(
                task_id,
                progress=15,
                message=t("progress.ontologySet"),
            )

            chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
            total_chunks = len(chunks)
            self.task_manager.update_task(
                task_id,
                progress=20,
                message=t("progress.textSplit", count=total_chunks),
            )

            self.add_text_batches(
                graph_id, chunks, batch_size,
                lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=20 + int(prog * 65),  # 20-85% (65 % Range)
                    message=msg,
                ),
            )

            self.task_manager.update_task(
                task_id,
                progress=90,
                message=t("progress.fetchingGraphInfo"),
            )

            graph_info = self._get_graph_info(graph_id)

            self.task_manager.complete_task(task_id, {
                "graph_id": graph_id,
                "graph_info": graph_info.to_dict(),
                "chunks_processed": total_chunks,
            })

        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            self.task_manager.fail_task(task_id, error_msg)

    # ------------------------------------------------------------------
    # Public synchrone API
    # ------------------------------------------------------------------

    def create_graph(self, name: str) -> str:
        """Erzeugt eine neue Graph-ID. Working-Dir + LightRAG-Instanz werden
        lazy beim ersten ``insert``/``set_ontology`` vom RagManager angelegt.

        ``name`` wird (bewusst) nicht persistiert — das war Zep-spezifische
        Metadata. Falls ein Display-Name gebraucht wird, sollte er in den
        Task-Metadata vom Caller mitgefuehrt werden.
        """
        return f"mirofish_{uuid.uuid4().hex[:16]}"

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        """Registriert die Ontologie als System-Prompt-Hint und persistiert sie.

        LightRAG kennt kein hartes Schema; die Ontologie wirkt nur als Hint
        fuer den Extraktor. Caller-API ist identisch zur Zep-Implementierung.
        """
        self.rag.set_ontology(graph_id, ontology)

    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None,
    ) -> List[str]:
        """Insertet alle Chunks synchron in den LightRAG-Graph.

        ``batch_size`` bestimmt nur noch die Granularitaet der Progress-Callbacks
        (LightRAG insertet pro Aufruf einen einzelnen Text — kein echter
        Batching-Vorteil mehr wie bei Zep). Liefert eine leere Liste zurueck —
        Episode-UUIDs gibt es bei LightRAG nicht.
        """
        total = len(chunks)
        if total == 0:
            return []

        for i, chunk in enumerate(chunks, start=1):
            try:
                self.rag.insert(graph_id, chunk)
            except Exception as e:
                if progress_callback:
                    progress_callback(
                        t("progress.batchFailed", batch=i, error=str(e)),
                        (i - 1) / total,
                    )
                raise

            if progress_callback and (i % max(batch_size, 1) == 0 or i == total):
                progress_callback(
                    t("progress.sendingBatch",
                      current=(i + batch_size - 1) // batch_size,
                      total=(total + batch_size - 1) // batch_size,
                      chunks=min(batch_size, i)),
                    i / total,
                )

        return []

    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """Aggregierte Graph-Statistik aus NetworkX-Storage."""
        nodes = self.rag.get_all_nodes(graph_id)
        edges = self.rag.get_all_edges(graph_id)

        entity_types: set[str] = set()
        for node in nodes:
            etype = _node_get(node, "entity_type", "type")
            if etype and etype not in ("Entity", "Node"):
                entity_types.add(str(etype))

        return GraphInfo(
            graph_id=graph_id,
            node_count=len(nodes),
            edge_count=len(edges),
            entity_types=list(entity_types),
        )

    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """Vollstaendige Graph-Daten fuer das Frontend (GraphPanel.vue).

        Mappt das LightRAG/NetworkX-Schema auf das Zep-Schema, das das
        Frontend erwartet. Felder, die LightRAG nicht kennt (``valid_at``,
        ``invalid_at``, ``expired_at``, ``episodes``), werden mit ``None``
        bzw. leeren Listen befuellt — Frontend-Renderer gehen davon aus.
        """
        nodes = self.rag.get_all_nodes(graph_id)
        edges = self.rag.get_all_edges(graph_id)

        # Map: entity_name -> uuid (deterministisch aus Name; LightRAG nutzt
        # den Namen selbst als ID im NetworkX-Graphen).
        nodes_data: List[Dict[str, Any]] = []
        name_to_uuid: Dict[str, str] = {}

        for node in nodes:
            entity_name = (
                _node_get(node, "entity_name", "name", "id", default="") or ""
            )
            if not entity_name:
                continue
            entity_type = _node_get(node, "entity_type", "type", default="") or ""
            description = _node_get(node, "description", "summary", default="") or ""
            source_id = _node_get(node, "source_id", default="") or ""

            # Wir verwenden entity_name als stabile UUID — LightRAG kennt
            # keine getrennten UUIDs, der Name ist primaerer Identifier.
            node_uuid = entity_name
            name_to_uuid[entity_name] = node_uuid

            nodes_data.append({
                "uuid": node_uuid,
                "name": entity_name,
                "labels": [entity_type] if entity_type else [],
                "summary": description,
                "attributes": {"source_id": source_id} if source_id else {},
                "created_at": None,
            })

        edges_data: List[Dict[str, Any]] = []
        for edge in edges:
            src = _edge_get(edge, "src_id", "source", default="") or ""
            tgt = _edge_get(edge, "tgt_id", "target", default="") or ""
            description = _edge_get(edge, "description", default="") or ""
            keywords = _edge_get(edge, "keywords", default="") or ""
            weight = _edge_get(edge, "weight", default=None)
            source_id = _edge_get(edge, "source_id", default="") or ""

            attributes: Dict[str, Any] = {}
            if weight is not None:
                attributes["weight"] = weight
            if keywords:
                attributes["keywords"] = keywords
            if source_id:
                attributes["source_id"] = source_id

            edges_data.append({
                "uuid": f"{src}__{tgt}",
                "name": keywords or "",
                "fact": description,
                "fact_type": keywords or "",
                "source_node_uuid": name_to_uuid.get(src, src),
                "target_node_uuid": name_to_uuid.get(tgt, tgt),
                "source_node_name": src,
                "target_node_name": tgt,
                "attributes": attributes,
                "created_at": None,
                "valid_at": None,
                "invalid_at": None,
                "expired_at": None,
                "episodes": [],
            })

        return {
            "graph_id": graph_id,
            "nodes": nodes_data,
            "edges": edges_data,
            "node_count": len(nodes_data),
            "edge_count": len(edges_data),
        }

    def delete_graph(self, graph_id: str) -> None:
        """Loescht die Graph-Instanz vollstaendig (Storages + working_dir)."""
        self.rag.delete(graph_id)
