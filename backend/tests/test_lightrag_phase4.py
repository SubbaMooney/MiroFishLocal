"""
Phase-4 Tests: GraphMemoryUpdater + Profile-Generator-Migration.

Validiert:
  - AgentActivity-Schema unveraendert vs. Zep-Variante
  - GraphMemoryUpdater throttling-Defaults aus Config
  - Batch-Insert via RagManager (gemockt)
  - GraphMemoryManager classmethods (Lifecycle)
  - OasisProfileGenerator._search_graph_for_entity nutzt EntityReader, nicht Zep
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.entity_reader import EntityNode, EntityReader
from app.services.graph_memory_updater import (
    AgentActivity,
    GraphMemoryManager,
    GraphMemoryUpdater,
)
from app.services.rag_manager import RagManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_rag(monkeypatch):
    fake = MagicMock()
    fake.insert.return_value = None
    monkeypatch.setattr(RagManager, "get_instance", classmethod(lambda cls: fake))
    return fake


@pytest.fixture(autouse=True)
def reset_manager():
    """Garantiert, dass jeder Test mit leerem GraphMemoryManager beginnt."""
    GraphMemoryManager.reset_for_test()
    yield
    GraphMemoryManager.reset_for_test()


# ---------------------------------------------------------------------------
# AgentActivity (Schema-Stabilitaet)
# ---------------------------------------------------------------------------


def test_agent_activity_create_post_text():
    a = AgentActivity(
        platform="twitter", agent_id=1, agent_name="alice",
        action_type="CREATE_POST", action_args={"content": "hello"},
        round_num=1, timestamp="2026-05-03T12:00:00",
    )
    assert a.to_episode_text() == "alice: 发布了一条帖子：「hello」"


def test_agent_activity_like_post_with_author():
    a = AgentActivity(
        platform="twitter", agent_id=1, agent_name="alice",
        action_type="LIKE_POST",
        action_args={"post_content": "Wetter heute", "post_author_name": "bob"},
        round_num=1, timestamp="2026-05-03T12:00:00",
    )
    assert "bob" in a.to_episode_text()
    assert "Wetter heute" in a.to_episode_text()


def test_agent_activity_unknown_type_falls_back_to_generic():
    a = AgentActivity(
        platform="twitter", agent_id=1, agent_name="alice",
        action_type="MYSTERY_ACTION", action_args={}, round_num=1, timestamp="x",
    )
    assert "MYSTERY_ACTION" in a.to_episode_text()


# ---------------------------------------------------------------------------
# Config-Defaults: aggressives Throttling
# ---------------------------------------------------------------------------


def test_throttling_defaults_aggressive():
    from app.config import Config

    # Defaults sollen 60x weniger Inserts ergeben als Zep-Originale (5/0.5s)
    assert Config.GRAPH_MEMORY_BATCH_SIZE == 50
    assert Config.GRAPH_MEMORY_SEND_INTERVAL == 30.0


def test_updater_picks_up_config_at_init(mock_rag, monkeypatch):
    from app.config import Config

    monkeypatch.setattr(Config, "GRAPH_MEMORY_BATCH_SIZE", 7, raising=False)
    monkeypatch.setattr(Config, "GRAPH_MEMORY_SEND_INTERVAL", 1.5, raising=False)

    updater = GraphMemoryUpdater("g1")
    assert updater.batch_size == 7
    assert updater.send_interval == 1.5


# ---------------------------------------------------------------------------
# GraphMemoryUpdater Lifecycle
# ---------------------------------------------------------------------------


def test_updater_skips_do_nothing(mock_rag):
    updater = GraphMemoryUpdater("g1")
    updater.add_activity(AgentActivity(
        platform="twitter", agent_id=1, agent_name="x",
        action_type="DO_NOTHING", action_args={},
        round_num=1, timestamp="t",
    ))
    stats = updater.get_stats()
    assert stats["skipped_count"] == 1
    assert stats["total_activities"] == 0


def test_updater_add_activity_from_dict_skips_event_entries(mock_rag):
    updater = GraphMemoryUpdater("g1")
    updater.add_activity_from_dict({"event_type": "round_start"}, "twitter")
    assert updater.get_stats()["total_activities"] == 0


def test_updater_send_batch_calls_rag_insert(mock_rag):
    updater = GraphMemoryUpdater("g1")
    activities = [
        AgentActivity(
            platform="twitter", agent_id=i, agent_name=f"u{i}",
            action_type="CREATE_POST", action_args={"content": f"post{i}"},
            round_num=1, timestamp="t",
        )
        for i in range(3)
    ]
    updater._send_batch_activities(activities, "twitter")
    mock_rag.insert.assert_called_once()
    args, _ = mock_rag.insert.call_args
    assert args[0] == "g1"
    assert "post0" in args[1]
    assert "post2" in args[1]
    assert updater.get_stats()["batches_sent"] == 1
    assert updater.get_stats()["items_sent"] == 3


def test_updater_retries_then_records_failure(mock_rag):
    mock_rag.insert.side_effect = RuntimeError("LLM down")
    updater = GraphMemoryUpdater("g1")
    # Speed up: keine Wait-Zeit zwischen Retries
    with patch.object(time, "sleep", lambda *_: None):
        updater._send_batch_activities([
            AgentActivity(
                platform="twitter", agent_id=1, agent_name="x",
                action_type="CREATE_POST", action_args={"content": "x"},
                round_num=1, timestamp="t",
            )
        ], "twitter")
    assert mock_rag.insert.call_count == GraphMemoryUpdater.MAX_RETRIES
    assert updater.get_stats()["failed_count"] == 1
    assert updater.get_stats()["batches_sent"] == 0


def test_updater_flush_remaining_drains_buffer(mock_rag):
    updater = GraphMemoryUpdater("g1")
    # 3 Activities, Batch-Size hoeher -> sollten in Buffer hängen, dann geflusht
    monkeypatch_size = 10
    updater.batch_size = monkeypatch_size
    for i in range(3):
        updater._platform_buffers["twitter"].append(AgentActivity(
            platform="twitter", agent_id=i, agent_name=f"u{i}",
            action_type="CREATE_POST", action_args={"content": f"p{i}"},
            round_num=1, timestamp="t",
        ))
    updater._flush_remaining()
    assert mock_rag.insert.call_count == 1
    assert updater.get_stats()["items_sent"] == 3
    assert all(len(b) == 0 for b in updater._platform_buffers.values())


# ---------------------------------------------------------------------------
# GraphMemoryManager Lifecycle
# ---------------------------------------------------------------------------


def test_manager_create_and_get(mock_rag):
    updater = GraphMemoryManager.create_updater("sim1", "g1")
    assert GraphMemoryManager.get_updater("sim1") is updater
    GraphMemoryManager.stop_updater("sim1")  # cleanup


def test_manager_create_replaces_existing(mock_rag):
    a = GraphMemoryManager.create_updater("sim1", "g1")
    b = GraphMemoryManager.create_updater("sim1", "g2")
    assert a is not b
    assert GraphMemoryManager.get_updater("sim1") is b
    GraphMemoryManager.stop_updater("sim1")


def test_manager_stop_updater_removes(mock_rag):
    GraphMemoryManager.create_updater("sim1", "g1")
    GraphMemoryManager.stop_updater("sim1")
    assert GraphMemoryManager.get_updater("sim1") is None


def test_manager_stop_all_idempotent(mock_rag):
    GraphMemoryManager.create_updater("sim1", "g1")
    GraphMemoryManager.create_updater("sim2", "g2")
    GraphMemoryManager.stop_all()
    GraphMemoryManager.stop_all()  # darf nicht crashen
    assert GraphMemoryManager.get_updater("sim1") is None
    assert GraphMemoryManager.get_updater("sim2") is None


def test_manager_get_all_stats(mock_rag):
    GraphMemoryManager.create_updater("sim1", "g1")
    stats = GraphMemoryManager.get_all_stats()
    assert "sim1" in stats
    assert stats["sim1"]["graph_id"] == "g1"
    GraphMemoryManager.stop_updater("sim1")


# ---------------------------------------------------------------------------
# OasisProfileGenerator._search_graph_for_entity (jetzt EntityReader-basiert)
# ---------------------------------------------------------------------------


def test_profile_search_returns_empty_without_graph_id(mock_rag, monkeypatch):
    """Ohne graph_id muss das Result leer sein."""
    monkeypatch.setattr("app.config.Config.LLM_API_KEY", "dummy", raising=False)
    monkeypatch.setattr("app.config.Config.LLM_MODEL_NAME", "test-model", raising=False)
    from app.services.oasis_profile_generator import OasisProfileGenerator

    gen = OasisProfileGenerator(api_key="dummy", graph_id=None)
    entity = EntityNode(uuid="alice", name="alice", labels=["PERSON"], summary="", attributes={})
    result = gen._search_graph_for_entity(entity)
    assert result == {"facts": [], "node_summaries": [], "context": ""}


def test_profile_search_uses_entity_reader(mock_rag, monkeypatch):
    """Mit graph_id ruft die Methode EntityReader.get_entity_with_context."""
    monkeypatch.setattr("app.config.Config.LLM_API_KEY", "dummy", raising=False)
    monkeypatch.setattr("app.config.Config.LLM_MODEL_NAME", "test-model", raising=False)
    from app.services.oasis_profile_generator import OasisProfileGenerator

    fake_reader = MagicMock(spec=EntityReader)
    fake_reader.get_entity_with_context.return_value = EntityNode(
        uuid="alice", name="alice", labels=["PERSON"], summary="lead",
        attributes={},
        related_edges=[
            {"direction": "outgoing", "edge_name": "works_at", "fact": "alice arbeitet bei acme", "target_node_uuid": "acme"},
            {"direction": "incoming", "edge_name": "manages", "fact": "carol managed alice", "source_node_uuid": "carol"},
        ],
        related_nodes=[
            {"uuid": "acme", "name": "acme", "labels": ["ORG"], "summary": "Tech Company"},
            {"uuid": "carol", "name": "carol", "labels": ["PERSON"], "summary": "CTO"},
        ],
    )
    monkeypatch.setattr(
        "app.services.oasis_profile_generator.EntityReader",
        lambda: fake_reader,
    )

    gen = OasisProfileGenerator(api_key="dummy", graph_id="g1")
    entity = EntityNode(uuid="alice", name="alice", labels=["PERSON"], summary="", attributes={})
    result = gen._search_graph_for_entity(entity)

    fake_reader.get_entity_with_context.assert_called_once_with("g1", "alice")
    assert "alice arbeitet bei acme" in result["facts"]
    assert "carol managed alice" in result["facts"]
    # Summary von acme + "相关实体: acme/carol" sind in node_summaries
    assert "Tech Company" in result["node_summaries"]
    assert "相关实体: acme" in result["node_summaries"]
    assert "事实信息" in result["context"]


def test_profile_search_handles_unknown_entity(mock_rag, monkeypatch):
    """EntityReader liefert None -> empty result, kein Crash."""
    monkeypatch.setattr("app.config.Config.LLM_API_KEY", "dummy", raising=False)
    monkeypatch.setattr("app.config.Config.LLM_MODEL_NAME", "test-model", raising=False)
    from app.services.oasis_profile_generator import OasisProfileGenerator

    fake_reader = MagicMock(spec=EntityReader)
    fake_reader.get_entity_with_context.return_value = None
    monkeypatch.setattr(
        "app.services.oasis_profile_generator.EntityReader",
        lambda: fake_reader,
    )

    gen = OasisProfileGenerator(api_key="dummy", graph_id="g1")
    entity = EntityNode(uuid="ghost", name="ghost", labels=["?"], summary="", attributes={})
    result = gen._search_graph_for_entity(entity)
    assert result == {"facts": [], "node_summaries": [], "context": ""}


def test_profile_constructor_no_zep_param(monkeypatch):
    """OasisProfileGenerator.__init__ darf keinen zep_api_key-Param mehr akzeptieren."""
    monkeypatch.setattr("app.config.Config.LLM_API_KEY", "dummy", raising=False)
    monkeypatch.setattr("app.config.Config.LLM_MODEL_NAME", "test-model", raising=False)
    from app.services.oasis_profile_generator import OasisProfileGenerator

    with pytest.raises(TypeError):
        OasisProfileGenerator(api_key="dummy", zep_api_key="should-not-exist")  # type: ignore[call-arg]
