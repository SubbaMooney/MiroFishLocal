"""
Phase-3a Tests: NetworkX-Mapping-Helpers + EntityReader.

Validiert das Drop-in-Replacement fuer ``ZepEntityReader``: gleiche Public-API
und gleiches Schema, aber NetworkX-basiert via ``RagManager``.

Reine Unit-Tests mit gemocktem RagManager — kein LightRAG, kein LLM.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services._networkx_mapping import (
    edge_to_dict,
    map_edges,
    map_nodes,
    node_to_dict,
)
from app.services.entity_reader import EntityNode, EntityReader, FilteredEntities
from app.services.rag_manager import RagManager


# ---------------------------------------------------------------------------
# _networkx_mapping helpers
# ---------------------------------------------------------------------------


def test_node_to_dict_dict_form():
    n = {
        "entity_name": "alice",
        "entity_type": "PERSON",
        "description": "Engineer",
        "source_id": "chunk_1",
    }
    d = node_to_dict(n)
    assert d["uuid"] == "alice"
    assert d["name"] == "alice"
    assert d["labels"] == ["PERSON"]
    assert d["summary"] == "Engineer"
    assert d["attributes"] == {"source_id": "chunk_1"}


def test_node_to_dict_tuple_form():
    n = ("alice", {"entity_type": "PERSON", "description": "Engineer"})
    d = node_to_dict(n)
    assert d["uuid"] == "alice"
    assert d["name"] == "alice"
    assert d["labels"] == ["PERSON"]


def test_node_to_dict_empty_inputs_safe():
    """Knoten ohne entity_name -> uuid/name leer, kein Crash."""
    d = node_to_dict({"entity_type": "PERSON"})
    assert d["uuid"] == ""
    assert d["name"] == ""
    assert d["labels"] == ["PERSON"]


def test_edge_to_dict_dict_form():
    e = {
        "src_id": "alice",
        "tgt_id": "bob",
        "description": "knows",
        "keywords": "social",
        "weight": 0.8,
    }
    d = edge_to_dict(e)
    assert d["uuid"] == "alice__bob"
    assert d["source_node_uuid"] == "alice"
    assert d["target_node_uuid"] == "bob"
    assert d["fact"] == "knows"
    assert d["name"] == "social"
    assert d["attributes"]["weight"] == 0.8
    assert d["attributes"]["keywords"] == "social"


def test_edge_to_dict_tuple_form():
    e = (("alice", "bob"), {"description": "knows"})
    d = edge_to_dict(e)
    assert d["source_node_uuid"] == "alice"
    assert d["target_node_uuid"] == "bob"
    assert d["fact"] == "knows"


def test_map_nodes_and_edges_round_trip():
    nodes = [{"entity_name": "alice", "entity_type": "PERSON"}]
    edges = [{"src_id": "alice", "tgt_id": "bob", "description": "knows"}]
    assert map_nodes(nodes)[0]["name"] == "alice"
    assert map_edges(edges)[0]["fact"] == "knows"


# ---------------------------------------------------------------------------
# EntityReader: Unit-Tests mit gemocktem RagManager
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_rag(monkeypatch):
    fake = MagicMock()
    fake.get_all_nodes.return_value = []
    fake.get_all_edges.return_value = []
    monkeypatch.setattr(RagManager, "get_instance", classmethod(lambda cls: fake))
    return fake


def test_entity_reader_constructor_no_args(mock_rag):
    """Konstruktor parameterlos — kein api_key noetig."""
    reader = EntityReader()
    assert reader.rag is mock_rag


def test_get_all_nodes_returns_canonical_schema(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON", "description": "x"},
    ]
    reader = EntityReader()
    nodes = reader.get_all_nodes("g1")
    assert nodes == [{
        "uuid": "alice",
        "name": "alice",
        "labels": ["PERSON"],
        "summary": "x",
        "attributes": {},
    }]


def test_get_all_edges_returns_canonical_schema(mock_rag):
    mock_rag.get_all_edges.return_value = [
        {"src_id": "alice", "tgt_id": "bob", "description": "knows"}
    ]
    reader = EntityReader()
    edges = reader.get_all_edges("g1")
    assert edges[0]["source_node_uuid"] == "alice"
    assert edges[0]["target_node_uuid"] == "bob"
    assert edges[0]["fact"] == "knows"


def test_get_node_edges_filters_by_endpoint(mock_rag):
    mock_rag.get_all_edges.return_value = [
        {"src_id": "alice", "tgt_id": "bob", "description": "knows"},
        {"src_id": "carol", "tgt_id": "alice", "description": "manages"},
        {"src_id": "carol", "tgt_id": "bob", "description": "unrelated"},
    ]
    reader = EntityReader()
    edges = reader.get_node_edges("g1", "alice")
    assert len(edges) == 2  # alice ist src in einem, tgt im anderen
    facts = {e["fact"] for e in edges}
    assert facts == {"knows", "manages"}


def test_filter_defined_entities_skips_generic_only(mock_rag):
    """Knoten ohne benutzerdefiniertes Label werden gefiltert."""
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
        {"entity_name": "x", "entity_type": "Entity"},  # generisch -> raus
        {"entity_name": "y", "entity_type": ""},  # leer -> raus
    ]
    mock_rag.get_all_edges.return_value = []
    reader = EntityReader()
    result = reader.filter_defined_entities("g1")
    assert isinstance(result, FilteredEntities)
    assert result.total_count == 3
    assert result.filtered_count == 1
    assert result.entities[0].name == "alice"
    assert "PERSON" in result.entity_types


def test_filter_defined_entities_with_type_whitelist(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
        {"entity_name": "acme", "entity_type": "ORG"},
        {"entity_name": "x", "entity_type": "PLACE"},
    ]
    mock_rag.get_all_edges.return_value = []
    reader = EntityReader()
    result = reader.filter_defined_entities("g1", defined_entity_types=["PERSON", "ORG"])
    assert result.filtered_count == 2
    names = {e.name for e in result.entities}
    assert names == {"alice", "acme"}


def test_filter_defined_entities_enriches_with_edges(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
        {"entity_name": "acme", "entity_type": "ORG"},
    ]
    mock_rag.get_all_edges.return_value = [
        {"src_id": "alice", "tgt_id": "acme", "description": "works at"}
    ]
    reader = EntityReader()
    result = reader.filter_defined_entities("g1", enrich_with_edges=True)
    alice = next(e for e in result.entities if e.name == "alice")
    assert len(alice.related_edges) == 1
    assert alice.related_edges[0]["direction"] == "outgoing"
    assert alice.related_edges[0]["fact"] == "works at"
    assert len(alice.related_nodes) == 1
    assert alice.related_nodes[0]["name"] == "acme"


def test_filter_defined_entities_no_enrichment(mock_rag):
    """``enrich_with_edges=False`` ueberspringt Edge-Lookup."""
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
    ]
    reader = EntityReader()
    result = reader.filter_defined_entities("g1", enrich_with_edges=False)
    assert result.entities[0].related_edges == []
    assert result.entities[0].related_nodes == []
    # get_all_edges sollte nicht aufgerufen worden sein
    mock_rag.get_all_edges.assert_not_called()


def test_get_entity_with_context_found(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON", "description": "lead"},
        {"entity_name": "acme", "entity_type": "ORG"},
    ]
    mock_rag.get_all_edges.return_value = [
        {"src_id": "alice", "tgt_id": "acme", "description": "works at"}
    ]
    reader = EntityReader()
    entity = reader.get_entity_with_context("g1", "alice")
    assert entity is not None
    assert entity.name == "alice"
    assert entity.summary == "lead"
    assert len(entity.related_edges) == 1
    assert entity.related_edges[0]["direction"] == "outgoing"
    assert len(entity.related_nodes) == 1
    assert entity.related_nodes[0]["name"] == "acme"


def test_get_entity_with_context_not_found(mock_rag):
    mock_rag.get_all_nodes.return_value = []
    reader = EntityReader()
    assert reader.get_entity_with_context("g1", "nope") is None


def test_get_entities_by_type_delegates(mock_rag):
    mock_rag.get_all_nodes.return_value = [
        {"entity_name": "alice", "entity_type": "PERSON"},
        {"entity_name": "acme", "entity_type": "ORG"},
    ]
    mock_rag.get_all_edges.return_value = []
    reader = EntityReader()
    persons = reader.get_entities_by_type("g1", "PERSON")
    assert len(persons) == 1
    assert persons[0].name == "alice"


def test_entity_node_get_entity_type():
    e = EntityNode(
        uuid="x", name="x", labels=["Entity", "PERSON", "Node"],
        summary="", attributes={},
    )
    assert e.get_entity_type() == "PERSON"


def test_entity_node_get_entity_type_no_custom():
    e = EntityNode(uuid="x", name="x", labels=["Entity"], summary="", attributes={})
    assert e.get_entity_type() is None


def test_entity_node_to_dict_round_trip():
    e = EntityNode(
        uuid="alice", name="alice", labels=["PERSON"],
        summary="lead", attributes={"x": 1},
        related_edges=[{"direction": "outgoing"}],
        related_nodes=[{"uuid": "bob"}],
    )
    d = e.to_dict()
    assert d["uuid"] == "alice"
    assert d["related_edges"][0]["direction"] == "outgoing"


def test_filtered_entities_to_dict():
    fe = FilteredEntities(
        entities=[EntityNode(uuid="x", name="x", labels=["A"], summary="", attributes={})],
        entity_types={"A"},
        total_count=10,
        filtered_count=1,
    )
    d = fe.to_dict()
    assert d["entity_types"] == ["A"]
    assert d["total_count"] == 10
    assert d["filtered_count"] == 1
