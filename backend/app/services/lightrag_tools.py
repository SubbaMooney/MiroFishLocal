"""
LightRAGToolsService — Tool-Schicht fuer den Report-Agent (Phase 3b).

Drop-in-Ersatz fuer ZepToolsService mit der Public-API, die der Report-Agent
schon konsumiert: ``quick_search``, ``panorama_search``, ``insight_forge``,
``interview_agents`` (delegiert an InterviewToolService).

Architektur-Wechsel (siehe docs/MIGRATION-ZEP-TO-LIGHTRAG.md):
- Alle Such-Tools lesen ueber ``RagManager`` aus dem lokalen NetworkX-Storage.
- ``quick_search`` ruft ``RagManager.query(mode="hybrid")`` (LLM-generierte
  Antwort als String).
- ``panorama_search`` arbeitet rein auf NetworkX (alle Edges), kein
  bitemporal-Tracking — Schema bleibt erhalten, aber ``historical_facts``
  ist immer leer (LightRAG kennt valid_at/invalid_at nicht).
- ``insight_forge`` parallelisiert Sub-Queries via ``ThreadPoolExecutor``
  (mehrere ``RagManager.query`` Calls landen alle auf demselben Loop, dort
  laufen sie konkurrierend als unabhaengige Coroutines).
- ``interview_agents`` ist OASIS-related und wird an ``InterviewToolService``
  delegiert (separates Modul, keine RAG-Dependency).

Datenklassen (``SearchResult``, ``NodeInfo``, ``EdgeInfo``,
``InsightForgeResult``, ``PanoramaResult``) behalten Schema und
``to_text()``-Format aus der Zep-Variante — der Report-Agent rendert
unveraendert.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..utils.llm_client import LLMClient
from ..utils.locale import t
from ..utils.logger import get_logger
from ._networkx_mapping import _edge_get, _node_get
from .interview_tool import InterviewResult, InterviewToolService
from .rag_manager import RagManager

logger = get_logger("mirofish.lightrag_tools")


# ---------------------------------------------------------------------------
# Datenklassen — Schema unveraendert vs. Zep-Variante (report_agent rendert
# weiterhin .to_text())
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    facts: List[str]
    edges: List[Dict[str, Any]]
    nodes: List[Dict[str, Any]]
    query: str
    total_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "facts": self.facts,
            "edges": self.edges,
            "nodes": self.nodes,
            "query": self.query,
            "total_count": self.total_count,
        }

    def to_text(self) -> str:
        text_parts = [f"搜索查询: {self.query}", f"找到 {self.total_count} 条相关信息"]
        if self.facts:
            text_parts.append("\n### 相关事实:")
            for i, fact in enumerate(self.facts, 1):
                text_parts.append(f"{i}. {fact}")
        return "\n".join(text_parts)


@dataclass
class NodeInfo:
    uuid: str
    name: str
    labels: List[str]
    summary: str
    attributes: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "labels": self.labels,
            "summary": self.summary,
            "attributes": self.attributes,
        }

    def to_text(self) -> str:
        entity_type = next(
            (l for l in self.labels if l not in ("Entity", "Node")), "未知类型"
        )
        return f"实体: {self.name} (类型: {entity_type})\n摘要: {self.summary}"


@dataclass
class EdgeInfo:
    uuid: str
    name: str
    fact: str
    source_node_uuid: str
    target_node_uuid: str
    source_node_name: Optional[str] = None
    target_node_name: Optional[str] = None
    # Phase 3a/b: LightRAG hat KEIN bitemporal Tracking. Felder bleiben fuer
    # Schema-Kompatibilitaet, sind aber immer None.
    created_at: Optional[str] = None
    valid_at: Optional[str] = None
    invalid_at: Optional[str] = None
    expired_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "fact": self.fact,
            "source_node_uuid": self.source_node_uuid,
            "target_node_uuid": self.target_node_uuid,
            "source_node_name": self.source_node_name,
            "target_node_name": self.target_node_name,
            "created_at": self.created_at,
            "valid_at": self.valid_at,
            "invalid_at": self.invalid_at,
            "expired_at": self.expired_at,
        }

    def to_text(self, include_temporal: bool = False) -> str:
        source = self.source_node_name or self.source_node_uuid[:8]
        target = self.target_node_name or self.target_node_uuid[:8]
        return f"关系: {source} --[{self.name}]--> {target}\n事实: {self.fact}"


@dataclass
class InsightForgeResult:
    query: str
    simulation_requirement: str
    sub_queries: List[str]
    semantic_facts: List[str] = field(default_factory=list)
    entity_insights: List[Dict[str, Any]] = field(default_factory=list)
    relationship_chains: List[str] = field(default_factory=list)
    total_facts: int = 0
    total_entities: int = 0
    total_relationships: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "simulation_requirement": self.simulation_requirement,
            "sub_queries": self.sub_queries,
            "semantic_facts": self.semantic_facts,
            "entity_insights": self.entity_insights,
            "relationship_chains": self.relationship_chains,
            "total_facts": self.total_facts,
            "total_entities": self.total_entities,
            "total_relationships": self.total_relationships,
        }

    def to_text(self) -> str:
        text_parts = [
            "## 未来预测深度分析",
            f"分析问题: {self.query}",
            f"预测场景: {self.simulation_requirement}",
            "\n### 预测数据统计",
            f"- 相关预测事实: {self.total_facts}条",
            f"- 涉及实体: {self.total_entities}个",
            f"- 关系链: {self.total_relationships}条",
        ]
        if self.sub_queries:
            text_parts.append("\n### 分析的子问题")
            for i, sq in enumerate(self.sub_queries, 1):
                text_parts.append(f"{i}. {sq}")
        if self.semantic_facts:
            text_parts.append("\n### 【关键事实】(请在报告中引用这些原文)")
            for i, fact in enumerate(self.semantic_facts, 1):
                text_parts.append(f'{i}. "{fact}"')
        if self.entity_insights:
            text_parts.append("\n### 【核心实体】")
            for entity in self.entity_insights:
                text_parts.append(
                    f"- **{entity.get('name', '未知')}** ({entity.get('type', '实体')})"
                )
                if entity.get("summary"):
                    text_parts.append(f'  摘要: "{entity.get("summary")}"')
                if entity.get("related_facts"):
                    text_parts.append(
                        f"  相关事实: {len(entity.get('related_facts', []))}条"
                    )
        if self.relationship_chains:
            text_parts.append("\n### 【关系链】")
            for chain in self.relationship_chains:
                text_parts.append(f"- {chain}")
        return "\n".join(text_parts)


@dataclass
class PanoramaResult:
    query: str
    all_nodes: List[NodeInfo] = field(default_factory=list)
    all_edges: List[EdgeInfo] = field(default_factory=list)
    active_facts: List[str] = field(default_factory=list)
    historical_facts: List[str] = field(default_factory=list)
    total_nodes: int = 0
    total_edges: int = 0
    active_count: int = 0
    historical_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "all_nodes": [n.to_dict() for n in self.all_nodes],
            "all_edges": [e.to_dict() for e in self.all_edges],
            "active_facts": self.active_facts,
            "historical_facts": self.historical_facts,
            "total_nodes": self.total_nodes,
            "total_edges": self.total_edges,
            "active_count": self.active_count,
            "historical_count": self.historical_count,
        }

    def to_text(self) -> str:
        text_parts = [
            "## 广度搜索结果（未来全景视图）",
            f"查询: {self.query}",
            "\n### 统计信息",
            f"- 总节点数: {self.total_nodes}",
            f"- 总边数: {self.total_edges}",
            f"- 当前有效事实: {self.active_count}条",
            f"- 历史/过期事实: {self.historical_count}条",
        ]
        if self.active_facts:
            text_parts.append("\n### 【当前有效事实】(模拟结果原文)")
            for i, fact in enumerate(self.active_facts, 1):
                text_parts.append(f'{i}. "{fact}"')
        if self.historical_facts:
            text_parts.append("\n### 【历史/过期事实】(演变过程记录)")
            for i, fact in enumerate(self.historical_facts, 1):
                text_parts.append(f'{i}. "{fact}"')
        if self.all_nodes:
            text_parts.append("\n### 【涉及实体】")
            for node in self.all_nodes:
                entity_type = next(
                    (l for l in node.labels if l not in ("Entity", "Node")), "实体"
                )
                text_parts.append(f"- **{node.name}** ({entity_type})")
        return "\n".join(text_parts)


# ---------------------------------------------------------------------------
# Interne Helper
# ---------------------------------------------------------------------------


def _extract_keywords(query: str) -> List[str]:
    """Triviale Keyword-Extraktion: tokenize + Kurzwoerter rausfiltern.

    Aequivalent zur Zep-Variante in _local_search/relevance_score.
    """
    return [w for w in query.lower().split() if len(w) > 1]


def _relevance_score(text: str, keywords: List[str]) -> int:
    """Trivial-Score: Anzahl der Keywords, die in `text` vorkommen."""
    if not text:
        return 0
    text_lower = text.lower()
    return sum(1 for kw in keywords if kw in text_lower)


def _name_for(uuid: str, node_map: Dict[str, str]) -> str:
    """Lookup-Helfer Name aus uuid; fallback auf erste 8 Zeichen der uuid."""
    return node_map.get(uuid) or (uuid[:8] if uuid else "")


# ---------------------------------------------------------------------------
# Hauptklasse
# ---------------------------------------------------------------------------


class LightRAGToolsService:
    """Tool-Bundle fuer den Report-Agent.

    Konstruktor nimmt optional einen vorinitialisierten ``LLMClient``
    (Tests koennen mocken). Die RagManager-Singleton-Instanz wird intern
    lazy bezogen.
    """

    DEFAULT_QUERY_MODE = "hybrid"
    INSIGHT_FORGE_PARALLELISM = 5

    def __init__(self, llm_client: Optional[LLMClient] = None) -> None:
        self.rag = RagManager.get_instance()
        self._llm = llm_client
        # Interview-Service lazy — vermeidet LLMClient-Instanziierung wenn
        # nur Such-Tools gebraucht werden.
        self._interview: Optional[InterviewToolService] = None

    @property
    def llm(self) -> LLMClient:
        """Lazy-LLM-Client (vermeidet Konstruktor-Zwang in Tests, die nur
        Such-Methoden ohne LLM brauchen)."""
        if self._llm is None:
            self._llm = LLMClient()
        return self._llm

    # ------------------------------------------------------------------
    # Tool 1: quick_search
    # ------------------------------------------------------------------

    def quick_search(
        self, graph_id: str, query: str, limit: int = 10
    ) -> SearchResult:
        """Einfache Hybrid-Suche. ``limit`` bleibt im Schema, hat aber bei
        LightRAG keinen direkten Effekt — die LLM-Antwort wird als
        einzelner ``fact`` zurueckgegeben.
        """
        try:
            answer = self.rag.query(graph_id, query, mode=self.DEFAULT_QUERY_MODE)
        except Exception as e:
            logger.warning("quick_search fehlgeschlagen: %s", e)
            return SearchResult(facts=[], edges=[], nodes=[], query=query, total_count=0)

        answer = (answer or "").strip()
        return SearchResult(
            facts=[answer] if answer else [],
            edges=[],
            nodes=[],
            query=query,
            total_count=1 if answer else 0,
        )

    # ------------------------------------------------------------------
    # Tool 2: panorama_search
    # ------------------------------------------------------------------

    def panorama_search(
        self,
        graph_id: str,
        query: str,
        include_expired: bool = False,
        limit: int = 50,
    ) -> PanoramaResult:
        """Breitsuche ueber den vollstaendigen Graphen.

        LightRAG hat KEIN bitemporal-Tracking (siehe Phase-2-Migration-Doc):
        - ``active_facts``: alle Edge-Facts, sortiert nach Keyword-Relevanz
        - ``historical_facts``: immer leer
        - ``include_expired``: Parameter bleibt fuer API-Kompat, ohne Effekt

        Edge-Facts werden auf ``limit`` truncated (sonst sprengt das Tool den
        LLM-Kontext bei grossen Graphen).
        """
        raw_nodes = self.rag.get_all_nodes(graph_id)
        raw_edges = self.rag.get_all_edges(graph_id)

        # uuid -> name Lookup fuer EdgeInfo.source_node_name/target_node_name
        node_map: Dict[str, str] = {}
        nodes: List[NodeInfo] = []
        for n in raw_nodes:
            entity_name = _node_get(n, "entity_name", "name", "id", default="") or ""
            entity_type = _node_get(n, "entity_type", "type", default="") or ""
            description = _node_get(n, "description", "summary", default="") or ""
            if not entity_name:
                continue
            node_map[entity_name] = entity_name
            nodes.append(
                NodeInfo(
                    uuid=entity_name,
                    name=entity_name,
                    labels=[entity_type] if entity_type else [],
                    summary=description,
                    attributes={},
                )
            )

        edges: List[EdgeInfo] = []
        for e in raw_edges:
            src = _edge_get(e, "src_id", "source", default="") or ""
            tgt = _edge_get(e, "tgt_id", "target", default="") or ""
            description = _edge_get(e, "description", default="") or ""
            keywords_str = _edge_get(e, "keywords", default="") or ""
            edges.append(
                EdgeInfo(
                    uuid=f"{src}__{tgt}" if src or tgt else "",
                    name=keywords_str,
                    fact=description,
                    source_node_uuid=src,
                    target_node_uuid=tgt,
                    source_node_name=_name_for(src, node_map),
                    target_node_name=_name_for(tgt, node_map),
                )
            )

        keywords = _extract_keywords(query)
        scored = sorted(
            edges,
            key=lambda e: _relevance_score(f"{e.fact} {e.name}", keywords),
            reverse=True,
        )
        active_facts = [e.fact for e in scored[:limit] if e.fact]

        return PanoramaResult(
            query=query,
            all_nodes=nodes,
            all_edges=edges,
            active_facts=active_facts,
            historical_facts=[],  # LightRAG: kein bitemporal
            total_nodes=len(nodes),
            total_edges=len(edges),
            active_count=len(active_facts),
            historical_count=0,
        )

    # ------------------------------------------------------------------
    # Tool 3: insight_forge (mit ThreadPool fuer parallele Sub-Queries)
    # ------------------------------------------------------------------

    def insight_forge(
        self,
        graph_id: str,
        query: str,
        simulation_requirement: str,
        report_context: Optional[str] = None,
        max_sub_queries: int = 5,
    ) -> InsightForgeResult:
        """Tiefen-Suche mit Sub-Query-Decomposition.

        Algorithmus:
          1. LLM generiert ``max_sub_queries`` Sub-Fragen.
          2. ALLE Queries (Haupt + Sub) parallel via ``RagManager.query``.
             ThreadPoolExecutor dispatcht nicht-blockierend; jede Coroutine
             laeuft auf dem RagManager._loop, der mehrere konkurrierend
             abarbeitet (single Loop, viele Coroutines).
          3. Aggregation: dedupliziere Antwort-Strings.
          4. Entity-Insights + Relationship-Chains aus NetworkX (kein LLM).
        """
        sub_queries = self._generate_sub_queries(
            query, simulation_requirement, report_context or "", max_sub_queries
        )

        all_queries = [query] + sub_queries
        parallelism = min(len(all_queries), self.INSIGHT_FORGE_PARALLELISM)
        with ThreadPoolExecutor(max_workers=parallelism) as executor:
            answers = list(
                executor.map(
                    lambda q: self._safe_query(graph_id, q),
                    all_queries,
                )
            )

        seen: set = set()
        semantic_facts: List[str] = []
        for a in answers:
            a_clean = (a or "").strip()
            if a_clean and a_clean not in seen:
                seen.add(a_clean)
                semantic_facts.append(a_clean)

        # Entity- und Relationship-Aggregation aus NetworkX
        raw_nodes = self.rag.get_all_nodes(graph_id)
        raw_edges = self.rag.get_all_edges(graph_id)

        entity_insights: List[Dict[str, Any]] = []
        for n in raw_nodes:
            entity_name = _node_get(n, "entity_name", "name", "id", default="") or ""
            if not entity_name:
                continue
            entity_type = _node_get(n, "entity_type", "type", default="") or ""
            description = _node_get(n, "description", "summary", default="") or ""
            related = [f for f in semantic_facts if entity_name.lower() in f.lower()]
            if related:
                entity_insights.append({
                    "name": entity_name,
                    "type": entity_type,
                    "summary": description,
                    "related_facts": related,
                })

        relationship_chains: List[str] = []
        for e in raw_edges:
            src = _edge_get(e, "src_id", "source", default="") or ""
            tgt = _edge_get(e, "tgt_id", "target", default="") or ""
            description = _edge_get(e, "description", default="") or ""
            if src and tgt:
                short_desc = description[:80] if description else ""
                relationship_chains.append(f"{src} --[{short_desc}]--> {tgt}")

        # Cap relationship_chains, damit der Prompt-Output handhabbar bleibt.
        relationship_chains = relationship_chains[:30]

        return InsightForgeResult(
            query=query,
            simulation_requirement=simulation_requirement,
            sub_queries=sub_queries,
            semantic_facts=semantic_facts,
            entity_insights=entity_insights,
            relationship_chains=relationship_chains,
            total_facts=len(semantic_facts),
            total_entities=len(entity_insights),
            total_relationships=len(relationship_chains),
        )

    def _safe_query(self, graph_id: str, query: str) -> str:
        """Wraps RagManager.query mit Exception-Schluck; fehlgeschlagene
        Sub-Queries duerfen das Aggregat nicht crashen."""
        try:
            return self.rag.query(graph_id, query, mode=self.DEFAULT_QUERY_MODE) or ""
        except Exception as e:
            logger.warning("insight_forge sub-query '%.60s' fehlgeschlagen: %s", query, e)
            return ""

    def _generate_sub_queries(
        self,
        query: str,
        simulation_requirement: str,
        report_context: str = "",
        max_queries: int = 5,
    ) -> List[str]:
        """LLM-Aufruf fuer Sub-Query-Decomposition (1:1 aus Zep-Variante)."""
        system_prompt = """你是一个专业的问题分析专家。你的任务是将一个复杂问题分解为多个可以在模拟世界中独立观察的子问题。

要求：
1. 每个子问题应该足够具体，可以在模拟世界中找到相关的Agent行为或事件
2. 子问题应该覆盖原问题的不同维度（如：谁、什么、为什么、怎么样、何时、何地）
3. 子问题应该与模拟场景相关
4. 返回JSON格式：{"sub_queries": ["子问题1", "子问题2", ...]}"""

        user_prompt = f"""模拟需求背景：
{simulation_requirement}

{f"报告上下文：{report_context[:500]}" if report_context else ""}

请将以下问题分解为{max_queries}个子问题：
{query}

返回JSON格式的子问题列表。"""

        try:
            response = self.llm.chat_json(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
            )
            sub_queries = response.get("sub_queries", [])
            return [str(sq) for sq in sub_queries[:max_queries]]
        except Exception as e:
            logger.warning(t("console.generateSubQueriesFailed", error=str(e)))
            return [
                query,
                f"{query} 的主要参与者",
                f"{query} 的原因和影响",
                f"{query} 的发展过程",
            ][:max_queries]

    # ------------------------------------------------------------------
    # Auxiliary Reads — vom Report-Agent intern verwendet (kein LLM-Tool-Call)
    # ------------------------------------------------------------------

    def _build_node_infos(self, graph_id: str) -> List[NodeInfo]:
        """Hilfs-Mapper: alle Knoten als NodeInfo-Liste."""
        infos: List[NodeInfo] = []
        for n in self.rag.get_all_nodes(graph_id):
            entity_name = _node_get(n, "entity_name", "name", "id", default="") or ""
            if not entity_name:
                continue
            entity_type = _node_get(n, "entity_type", "type", default="") or ""
            description = _node_get(n, "description", "summary", default="") or ""
            infos.append(NodeInfo(
                uuid=entity_name,
                name=entity_name,
                labels=[entity_type] if entity_type else [],
                summary=description,
                attributes={},
            ))
        return infos

    def get_entities_by_type(
        self, graph_id: str, entity_type: str
    ) -> List[NodeInfo]:
        """Filtert Knoten anhand des Labels (matched gegen entity_type)."""
        return [n for n in self._build_node_infos(graph_id) if entity_type in n.labels]

    def get_entity_summary(
        self, graph_id: str, entity_name: str
    ) -> Dict[str, Any]:
        """Aggregiert verwandte Edges + Quick-Search-Antwort fuer eine Entitaet."""
        nodes = self._build_node_infos(graph_id)
        entity_node = next(
            (n for n in nodes if n.name.lower() == entity_name.lower()), None
        )

        related_edges: List[Dict[str, Any]] = []
        if entity_node:
            for e in self.rag.get_all_edges(graph_id):
                src = _edge_get(e, "src_id", "source", default="") or ""
                tgt = _edge_get(e, "tgt_id", "target", default="") or ""
                if src == entity_node.uuid or tgt == entity_node.uuid:
                    related_edges.append({
                        "source_node_uuid": src,
                        "target_node_uuid": tgt,
                        "fact": _edge_get(e, "description", default="") or "",
                        "name": _edge_get(e, "keywords", default="") or "",
                    })

        search = self.quick_search(graph_id, entity_name)

        return {
            "entity_name": entity_name,
            "entity_info": entity_node.to_dict() if entity_node else None,
            "related_facts": search.facts,
            "related_edges": related_edges,
            "total_relations": len(related_edges),
        }

    def get_graph_statistics(self, graph_id: str) -> Dict[str, Any]:
        """Verteilung der Entity- und Relation-Typen."""
        raw_nodes = self.rag.get_all_nodes(graph_id)
        raw_edges = self.rag.get_all_edges(graph_id)

        entity_types: Dict[str, int] = {}
        for n in raw_nodes:
            etype = _node_get(n, "entity_type", "type", default="") or ""
            if etype and etype not in ("Entity", "Node"):
                entity_types[etype] = entity_types.get(etype, 0) + 1

        relation_types: Dict[str, int] = {}
        for e in raw_edges:
            rname = _edge_get(e, "keywords", default="") or ""
            relation_types[rname] = relation_types.get(rname, 0) + 1

        return {
            "graph_id": graph_id,
            "total_nodes": len(raw_nodes),
            "total_edges": len(raw_edges),
            "entity_types": entity_types,
            "relation_types": relation_types,
        }

    def get_simulation_context(
        self,
        graph_id: str,
        simulation_requirement: str,
        limit: int = 30,
    ) -> Dict[str, Any]:
        """Bundle aus Quick-Search-Antwort + Graph-Stats + Entity-Liste."""
        search = self.quick_search(graph_id, simulation_requirement, limit=limit)
        stats = self.get_graph_statistics(graph_id)

        entities: List[Dict[str, Any]] = []
        for n in self._build_node_infos(graph_id):
            custom_labels = [l for l in n.labels if l not in ("Entity", "Node")]
            if custom_labels:
                entities.append({
                    "name": n.name,
                    "type": custom_labels[0],
                    "summary": n.summary,
                })

        return {
            "simulation_requirement": simulation_requirement,
            "related_facts": search.facts,
            "graph_statistics": stats,
            "entities": entities[:limit],
        }

    # ------------------------------------------------------------------
    # Tool 4: interview_agents (delegiert an InterviewToolService)
    # ------------------------------------------------------------------

    def interview_agents(
        self,
        simulation_id: str,
        interview_requirement: str,
        simulation_requirement: str = "",
        max_agents: int = 5,
        custom_questions: Optional[List[str]] = None,
    ) -> InterviewResult:
        """Delegiert an InterviewToolService — OASIS-related, keine RAG-Calls."""
        if self._interview is None:
            self._interview = InterviewToolService(llm_client=self._llm)
        return self._interview.interview_agents(
            simulation_id=simulation_id,
            interview_requirement=interview_requirement,
            simulation_requirement=simulation_requirement,
            max_agents=max_agents,
            custom_questions=custom_questions,
        )


__all__ = [
    "SearchResult",
    "NodeInfo",
    "EdgeInfo",
    "InsightForgeResult",
    "PanoramaResult",
    "LightRAGToolsService",
]
