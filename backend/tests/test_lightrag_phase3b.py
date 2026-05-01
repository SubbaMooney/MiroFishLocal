"""
Phase-3b Tests: LightRAGToolsService (3 RAG-Tools + Auxiliary Reads).

Mockt RagManager + LLMClient — kein LightRAG, kein echter LLM-Call.
interview_agents wird nicht direkt getestet; das ist Verantwortung der
InterviewToolService-Tests (Phase 3b folgt-up oder eigenstaendig).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.lightrag_tools import (
    EdgeInfo,
    InsightForgeResult,
    LightRAGToolsService,
    NodeInfo,
    PanoramaResult,
    SearchResult,
    _extract_keywords,
    _relevance_score,
)
from app.services.rag_manager import RagManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_rag(monkeypatch):
    fake = MagicMock()
    fake.get_all_nodes.return_value = []
    fake.get_all_edges.return_value = []
    fake.query.return_value = ""
    monkeypatch.setattr(RagManager, "get_instance", classmethod(lambda cls: fake))
    return fake


@pytest.fixture
def mock_llm():
    """LLMClient-Mock fuer _generate_sub_queries."""
    fake = MagicMock()
    fake.chat_json.return_value = {"sub_queries": ["sub1", "sub2", "sub3"]}
    return fake


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_extract_keywords_filters_short():
    assert _extract_keywords("how is alice doing today") == ["how", "is", "alice", "doing", "today"]
    # Single-character tokens removed:
    assert _extract_keywords("a b c xyz") == ["xyz"]


def test_relevance_score_counts_keyword_overlap():
    assert _relevance_score("alice works at acme", ["alice", "missing"]) == 1
    assert _relevance_score("alice works at acme", ["alice", "acme", "missing"]) == 2
    assert _relevance_score("", ["alice"]) == 0


# ---------------------------------------------------------------------------
# quick_search
# ---------------------------------------------------------------------------


def test_quick_search_wraps_rag_answer(mock_rag):
    mock_rag.query.return_value = "Alice ist Engineering-Lead bei Acme."
    tools = LightRAGToolsService(llm_client=MagicMock())
    result = tools.quick_search("g1", "Wer ist Alice?")
    assert isinstance(result, SearchResult)
    assert result.facts == ["Alice ist Engineering-Lead bei Acme."]
    assert result.total_count == 1
    assert result.query == "Wer ist Alice?"
    mock_rag.query.assert_called_once_with("g1", "Wer ist Alice?", mode="hybrid")


def test_quick_search_empty_answer_zero_facts(mock_rag):
    mock_rag.query.return_value = ""
    tools = LightRAGToolsService(llm_client=MagicMock())
    result = tools.quick_search("g1", "?")
    assert result.facts == []
    assert result.total_count == 0


def test_quick_search_handles_query_exception(mock_rag):
    mock_rag.query.side_effect = RuntimeError("LLM down")
    tools = LightRAGToolsService(llm_client=MagicMock())
    result = tools.quick_search("g1", "x")
    assert result.facts == []
    assert result.total_count == 0


# ---------------------------------------------------------------------------
# panorama_search
# ---------------------------------------------------------------------------


def test_panorama_search_no_temporal_split(mock_rag):
    """LightRAG kennt kein bitemporal — historical_facts MUSS leer sein."""
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
    ]
    mock_rag.get_all_edges.return_value = [
        {"src_id": "alice", "tgt_id": "bob", "description": "knows"},
    ]
    tools = LightRAGToolsService(llm_client=MagicMock())
    result = tools.panorama_search("g1", "alice")
    assert isinstance(result, PanoramaResult)
    assert result.historical_facts == []
    assert result.historical_count == 0
    assert "knows" in result.active_facts


def test_panorama_search_sorts_by_relevance(mock_rag):
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = [
        {"src_id": "a", "tgt_id": "b", "description": "boring trivial fact"},
        {"src_id": "c", "tgt_id": "d", "description": "alice founded acme"},
        {"src_id": "e", "tgt_id": "f", "description": "alice is happy"},
    ]
    tools = LightRAGToolsService(llm_client=MagicMock())
    result = tools.panorama_search("g1", "alice acme")
    # Edge mit beiden Keywords ("alice founded acme") sollte first sein
    assert result.active_facts[0] == "alice founded acme"


def test_panorama_search_respects_limit(mock_rag):
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = [
        {"src_id": f"a{i}", "tgt_id": f"b{i}", "description": f"fact {i}"}
        for i in range(100)
    ]
    tools = LightRAGToolsService(llm_client=MagicMock())
    result = tools.panorama_search("g1", "fact", limit=10)
    assert len(result.active_facts) == 10


def test_panorama_search_node_info_schema(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON", "description": "lead"},
    ]
    mock_rag.get_all_edges.return_value = []
    tools = LightRAGToolsService(llm_client=MagicMock())
    result = tools.panorama_search("g1", "x")
    assert len(result.all_nodes) == 1
    n = result.all_nodes[0]
    assert isinstance(n, NodeInfo)
    assert n.uuid == "alice"
    assert n.labels == ["PERSON"]
    assert n.summary == "lead"


# ---------------------------------------------------------------------------
# insight_forge
# ---------------------------------------------------------------------------


def test_insight_forge_runs_subqueries_in_parallel(mock_rag, mock_llm):
    """ThreadPool: alle (haupt + sub) queries werden tatsaechlich abgesetzt."""
    mock_rag.query.return_value = "answer"
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = []

    tools = LightRAGToolsService(llm_client=mock_llm)
    result = tools.insight_forge("g1", "main_q", "sim_req", max_sub_queries=3)

    # 1 main + 3 sub_queries = 4 query-Calls
    assert mock_rag.query.call_count == 4
    assert result.sub_queries == ["sub1", "sub2", "sub3"]


def test_insight_forge_deduplicates_facts(mock_rag, mock_llm):
    mock_rag.query.return_value = "duplicate_answer"
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = []
    tools = LightRAGToolsService(llm_client=mock_llm)
    result = tools.insight_forge("g1", "q", "req", max_sub_queries=3)
    assert result.semantic_facts == ["duplicate_answer"]
    assert result.total_facts == 1


def test_insight_forge_extracts_entity_insights(mock_rag, mock_llm):
    mock_rag.query.side_effect = [
        "Alice gruendet Acme.",
        "Bob arbeitet auch bei Acme.",
        "Niemand kennt Carol.",
    ]
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "Alice", "entity_type": "PERSON", "description": "Founder"},
        {"entity_name": "Carol", "entity_type": "PERSON", "description": "?"},
    ]
    mock_rag.get_all_edges.return_value = []
    mock_llm.chat_json.return_value = {"sub_queries": ["sub1", "sub2"]}

    tools = LightRAGToolsService(llm_client=mock_llm)
    result = tools.insight_forge("g1", "q", "req", max_sub_queries=2)
    # Alice taucht in 1 Antwort auf -> entity_insights[0]; Carol in 1 Antwort -> entity_insights[1]
    names = {ei["name"] for ei in result.entity_insights}
    assert names == {"Alice", "Carol"}


def test_insight_forge_builds_relationship_chains(mock_rag, mock_llm):
    mock_rag.query.return_value = "answer"
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = [
        {"src_id": "alice", "tgt_id": "acme", "description": "founded"},
        {"src_id": "bob", "tgt_id": "acme", "description": "works at"},
    ]
    tools = LightRAGToolsService(llm_client=mock_llm)
    result = tools.insight_forge("g1", "q", "req", max_sub_queries=1)
    chains = result.relationship_chains
    assert any("alice" in c and "acme" in c for c in chains)


def test_insight_forge_caps_relationship_chains(mock_rag, mock_llm):
    mock_rag.query.return_value = "answer"
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = [
        {"src_id": f"a{i}", "tgt_id": f"b{i}", "description": "x"} for i in range(50)
    ]
    tools = LightRAGToolsService(llm_client=mock_llm)
    result = tools.insight_forge("g1", "q", "req")
    assert len(result.relationship_chains) == 30  # cap


def test_insight_forge_subquery_failure_does_not_crash(mock_rag, mock_llm):
    """Wenn der LLM-Call fuer Sub-Queries fehlschlaegt: Fallback-Variante."""
    mock_llm.chat_json.side_effect = RuntimeError("LLM offline")
    mock_rag.query.return_value = "answer"
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = []

    tools = LightRAGToolsService(llm_client=mock_llm)
    result = tools.insight_forge("g1", "what about X?", "sim_req", max_sub_queries=4)
    # Fallback liefert (max_count) Variations
    assert len(result.sub_queries) == 4
    assert result.sub_queries[0] == "what about X?"


# ---------------------------------------------------------------------------
# Auxiliary Reads
# ---------------------------------------------------------------------------


def test_get_entities_by_type(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
        {"entity_name": "acme", "entity_type": "ORG"},
    ]
    tools = LightRAGToolsService(llm_client=MagicMock())
    persons = tools.get_entities_by_type("g1", "PERSON")
    assert len(persons) == 1
    assert persons[0].name == "alice"
    assert isinstance(persons[0], NodeInfo)


def test_get_entity_summary(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON", "description": "lead"},
    ]
    mock_rag.get_all_edges.return_value = [
        {"src_id": "alice", "tgt_id": "acme", "description": "works at"},
        {"src_id": "carol", "tgt_id": "alice", "description": "manages"},
    ]
    mock_rag.query.return_value = "Alice ist Lead."
    tools = LightRAGToolsService(llm_client=MagicMock())
    summary = tools.get_entity_summary("g1", "alice")
    assert summary["entity_name"] == "alice"
    assert summary["entity_info"]["name"] == "alice"
    assert summary["total_relations"] == 2
    assert summary["related_facts"] == ["Alice ist Lead."]


def test_get_entity_summary_unknown_entity(mock_rag):
    mock_rag.get_all_nodes.return_value = []
    mock_rag.get_all_edges.return_value = []
    mock_rag.query.return_value = ""
    tools = LightRAGToolsService(llm_client=MagicMock())
    summary = tools.get_entity_summary("g1", "ghost")
    assert summary["entity_info"] is None
    assert summary["total_relations"] == 0


def test_get_graph_statistics(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "a", "entity_type": "PERSON"},
        {"entity_name": "b", "entity_type": "PERSON"},
        {"entity_name": "c", "entity_type": "ORG"},
        {"entity_name": "x", "entity_type": "Entity"},  # generisch -> nicht gezaehlt
    ]
    mock_rag.get_all_edges.return_value = [
        {"src_id": "a", "tgt_id": "b", "keywords": "knows"},
        {"src_id": "a", "tgt_id": "c", "keywords": "works_at"},
    ]
    tools = LightRAGToolsService(llm_client=MagicMock())
    stats = tools.get_graph_statistics("g1")
    assert stats["total_nodes"] == 4
    assert stats["total_edges"] == 2
    assert stats["entity_types"] == {"PERSON": 2, "ORG": 1}
    assert stats["relation_types"] == {"knows": 1, "works_at": 1}


def test_get_simulation_context_bundles_quick_search_and_stats(mock_rag):
    mock_rag.query.return_value = "Sim-Kontext-Antwort."
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON", "description": "lead"},
    ]
    mock_rag.get_all_edges.return_value = []
    tools = LightRAGToolsService(llm_client=MagicMock())
    ctx = tools.get_simulation_context("g1", "Was passiert bei X?", limit=10)
    assert ctx["simulation_requirement"] == "Was passiert bei X?"
    assert ctx["related_facts"] == ["Sim-Kontext-Antwort."]
    assert ctx["graph_statistics"]["total_nodes"] == 1
    assert ctx["entities"][0]["name"] == "alice"


# ---------------------------------------------------------------------------
# interview_agents (delegation only — full coverage in interview_tool tests)
# ---------------------------------------------------------------------------


def test_interview_agents_delegates(monkeypatch, mock_rag):
    """Stellt sicher, dass LightRAGToolsService.interview_agents an
    InterviewToolService delegiert."""
    fake_interview_service = MagicMock()
    fake_interview_service.interview_agents.return_value = MagicMock(spec_set=[])
    monkeypatch.setattr(
        "app.services.lightrag_tools.InterviewToolService",
        lambda llm_client=None: fake_interview_service,
    )

    tools = LightRAGToolsService(llm_client=MagicMock())
    tools.interview_agents("sim1", "interview-req", "sim-req", max_agents=3)
    fake_interview_service.interview_agents.assert_called_once_with(
        simulation_id="sim1",
        interview_requirement="interview-req",
        simulation_requirement="sim-req",
        max_agents=3,
        custom_questions=None,
    )


# ---------------------------------------------------------------------------
# Datenklassen-Smoke-Tests (round-trips)
# ---------------------------------------------------------------------------


def test_search_result_to_text():
    sr = SearchResult(facts=["x", "y"], edges=[], nodes=[], query="q", total_count=2)
    text = sr.to_text()
    assert "q" in text
    assert "找到 2" in text
    assert "1. x" in text


def test_node_info_to_text_skips_generic_labels():
    n = NodeInfo(uuid="u", name="alice", labels=["Entity", "PERSON"], summary="x", attributes={})
    assert "PERSON" in n.to_text()
    assert "alice" in n.to_text()


def test_edge_info_to_text_with_names():
    e = EdgeInfo(
        uuid="u", name="knows", fact="alice kennt bob",
        source_node_uuid="a-uuid-12345", target_node_uuid="b-uuid-67890",
        source_node_name="alice", target_node_name="bob",
    )
    assert "alice" in e.to_text()
    assert "bob" in e.to_text()


def test_panorama_result_to_dict_round_trip():
    pr = PanoramaResult(
        query="q",
        all_nodes=[NodeInfo(uuid="u", name="a", labels=["X"], summary="", attributes={})],
        all_edges=[],
        active_facts=["f1"],
        historical_facts=[],
        total_nodes=1, total_edges=0, active_count=1, historical_count=0,
    )
    d = pr.to_dict()
    assert d["historical_count"] == 0
    assert d["all_nodes"][0]["name"] == "a"


def test_insight_forge_result_to_text():
    ifr = InsightForgeResult(
        query="q", simulation_requirement="r", sub_queries=["s1"],
        semantic_facts=["f1"], entity_insights=[{"name": "alice", "type": "PERSON"}],
        relationship_chains=["a->b"],
        total_facts=1, total_entities=1, total_relationships=1,
    )
    text = ifr.to_text()
    assert "f1" in text
    assert "alice" in text
    assert "a->b" in text
