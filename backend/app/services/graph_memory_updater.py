"""
Graph-Memory-Updater — schreibt Agent-Aktivitaeten waehrend laufender
Simulation in den LightRAG-Wissensgraphen.

Phase 4 der Zep->LightRAG-Migration (siehe docs/MIGRATION-ZEP-TO-LIGHTRAG.md).

Drop-in-Ersatz fuer den alten ``ZepGraphMemoryUpdater``: gleiche Public-API
(``start``/``stop``/``add_activity``/``add_activity_from_dict``/``get_stats``),
aber Inserts laufen ueber ``RagManager`` statt ``Zep.client.graph.add``.

KOSTEN-WICHTIG: Bei Zep war jeder Insert ein billiger Episode-API-Call. Bei
LightRAG ist jeder Insert eine **volle LLM-Extraktion**. Daher Throttling
vom Default 0.5s/5 (600 Activities/min) auf 30s/50 (~60x weniger Inserts) —
siehe Config.GRAPH_MEMORY_BATCH_SIZE / GRAPH_MEMORY_SEND_INTERVAL.

AgentActivity bleibt schemakompatibel — gleiche to_episode_text-Logik,
damit nachgelagerte Caller (action_logger, jsonl-Files) ohne Anpassung
weiterlaufen.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Dict, List, Optional

from ..config import Config
from ..utils.locale import get_locale, set_locale
from ..utils.logger import get_logger
from .rag_manager import RagManager

logger = get_logger("mirofish.graph_memory_updater")


# ---------------------------------------------------------------------------
# AgentActivity — pure dataclass, identisch zur Zep-Variante (kein Zep-Bezug)
# ---------------------------------------------------------------------------


@dataclass
class AgentActivity:
    """Agent-Aktivitaetseintrag.

    Felder + ``to_episode_text()``-Format unveraendert vs. Zep-Implementierung,
    damit Caller (action_logger, JSONL-Replay) ohne Aenderung weiterlaufen.
    """
    platform: str
    agent_id: int
    agent_name: str
    action_type: str
    action_args: Dict[str, Any]
    round_num: int
    timestamp: str

    def to_episode_text(self) -> str:
        """Natuerlich-sprachliche Beschreibung der Aktivitaet — wird an
        LightRAG zur Entity/Relation-Extraktion gesendet."""
        action_descriptions = {
            "CREATE_POST": self._describe_create_post,
            "LIKE_POST": self._describe_like_post,
            "DISLIKE_POST": self._describe_dislike_post,
            "REPOST": self._describe_repost,
            "QUOTE_POST": self._describe_quote_post,
            "FOLLOW": self._describe_follow,
            "CREATE_COMMENT": self._describe_create_comment,
            "LIKE_COMMENT": self._describe_like_comment,
            "DISLIKE_COMMENT": self._describe_dislike_comment,
            "SEARCH_POSTS": self._describe_search,
            "SEARCH_USER": self._describe_search_user,
            "MUTE": self._describe_mute,
        }
        describe = action_descriptions.get(self.action_type, self._describe_generic)
        return f"{self.agent_name}: {describe()}"

    def _describe_create_post(self) -> str:
        content = self.action_args.get("content", "")
        return f"发布了一条帖子：「{content}」" if content else "发布了一条帖子"

    def _describe_like_post(self) -> str:
        content = self.action_args.get("post_content", "")
        author = self.action_args.get("post_author_name", "")
        if content and author:
            return f"点赞了{author}的帖子：「{content}」"
        if content:
            return f"点赞了一条帖子：「{content}」"
        if author:
            return f"点赞了{author}的一条帖子"
        return "点赞了一条帖子"

    def _describe_dislike_post(self) -> str:
        content = self.action_args.get("post_content", "")
        author = self.action_args.get("post_author_name", "")
        if content and author:
            return f"踩了{author}的帖子：「{content}」"
        if content:
            return f"踩了一条帖子：「{content}」"
        if author:
            return f"踩了{author}的一条帖子"
        return "踩了一条帖子"

    def _describe_repost(self) -> str:
        content = self.action_args.get("original_content", "")
        author = self.action_args.get("original_author_name", "")
        if content and author:
            return f"转发了{author}的帖子：「{content}」"
        if content:
            return f"转发了一条帖子：「{content}」"
        if author:
            return f"转发了{author}的一条帖子"
        return "转发了一条帖子"

    def _describe_quote_post(self) -> str:
        original = self.action_args.get("original_content", "")
        author = self.action_args.get("original_author_name", "")
        quote = self.action_args.get("quote_content", "") or self.action_args.get("content", "")
        if original and author:
            base = f"引用了{author}的帖子「{original}」"
        elif original:
            base = f"引用了一条帖子「{original}」"
        elif author:
            base = f"引用了{author}的一条帖子"
        else:
            base = "引用了一条帖子"
        if quote:
            base += f"，并评论道：「{quote}」"
        return base

    def _describe_follow(self) -> str:
        target = self.action_args.get("target_user_name", "")
        return f"关注了用户「{target}」" if target else "关注了一个用户"

    def _describe_create_comment(self) -> str:
        content = self.action_args.get("content", "")
        post_content = self.action_args.get("post_content", "")
        post_author = self.action_args.get("post_author_name", "")
        if not content:
            return "发表了评论"
        if post_content and post_author:
            return f"在{post_author}的帖子「{post_content}」下评论道：「{content}」"
        if post_content:
            return f"在帖子「{post_content}」下评论道：「{content}」"
        if post_author:
            return f"在{post_author}的帖子下评论道：「{content}」"
        return f"评论道：「{content}」"

    def _describe_like_comment(self) -> str:
        content = self.action_args.get("comment_content", "")
        author = self.action_args.get("comment_author_name", "")
        if content and author:
            return f"点赞了{author}的评论：「{content}」"
        if content:
            return f"点赞了一条评论：「{content}」"
        if author:
            return f"点赞了{author}的一条评论"
        return "点赞了一条评论"

    def _describe_dislike_comment(self) -> str:
        content = self.action_args.get("comment_content", "")
        author = self.action_args.get("comment_author_name", "")
        if content and author:
            return f"踩了{author}的评论：「{content}」"
        if content:
            return f"踩了一条评论：「{content}」"
        if author:
            return f"踩了{author}的一条评论"
        return "踩了一条评论"

    def _describe_search(self) -> str:
        query = self.action_args.get("query", "") or self.action_args.get("keyword", "")
        return f"搜索了「{query}」" if query else "进行了搜索"

    def _describe_search_user(self) -> str:
        query = self.action_args.get("query", "") or self.action_args.get("username", "")
        return f"搜索了用户「{query}」" if query else "搜索了用户"

    def _describe_mute(self) -> str:
        target = self.action_args.get("target_user_name", "")
        return f"屏蔽了用户「{target}」" if target else "屏蔽了一个用户"

    def _describe_generic(self) -> str:
        return f"执行了{self.action_type}操作"


# ---------------------------------------------------------------------------
# GraphMemoryUpdater — LightRAG-backed
# ---------------------------------------------------------------------------


class GraphMemoryUpdater:
    """LightRAG-backed Agent-Activity-Updater fuer eine Simulation.

    Public-API identisch zur Zep-Variante. Throttling-Defaults aggressiv
    (siehe Config.GRAPH_MEMORY_BATCH_SIZE / GRAPH_MEMORY_SEND_INTERVAL),
    weil jeder Insert hier ein voller LLM-Extraktions-Pass ist.
    """

    PLATFORM_DISPLAY_NAMES = {
        "twitter": "世界1",
        "reddit": "世界2",
    }
    MAX_RETRIES = 3
    RETRY_DELAY = 2.0  # Sekunden, exponential

    def __init__(self, graph_id: str) -> None:
        self.graph_id = graph_id
        self.rag = RagManager.get_instance()

        # Throttling-Werte aus Config (zur Laufzeit lesen — Tests koennen patchen).
        self.batch_size = Config.GRAPH_MEMORY_BATCH_SIZE
        self.send_interval = Config.GRAPH_MEMORY_SEND_INTERVAL

        self._activity_queue: Queue = Queue()
        self._platform_buffers: Dict[str, List[AgentActivity]] = {
            "twitter": [],
            "reddit": [],
        }
        self._buffer_lock = threading.Lock()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

        # Stats
        self._total_activities = 0
        self._total_sent = 0
        self._total_items_sent = 0
        self._failed_count = 0
        self._skipped_count = 0

        logger.info(
            "GraphMemoryUpdater init: graph_id=%s, batch_size=%d, send_interval=%.1fs",
            graph_id, self.batch_size, self.send_interval,
        )

    def _get_platform_display_name(self, platform: str) -> str:
        return self.PLATFORM_DISPLAY_NAMES.get(platform.lower(), platform)

    def start(self) -> None:
        if self._running:
            return
        current_locale = get_locale()
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            args=(current_locale,),
            daemon=True,
            name=f"GraphMemoryUpdater-{self.graph_id[:8]}",
        )
        self._worker_thread.start()
        logger.info("GraphMemoryUpdater gestartet: graph_id=%s", self.graph_id)

    def stop(self) -> None:
        self._running = False
        self._flush_remaining()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)
        logger.info(
            "GraphMemoryUpdater gestoppt: graph_id=%s, total=%d, batches=%d, items=%d, failed=%d, skipped=%d",
            self.graph_id, self._total_activities, self._total_sent,
            self._total_items_sent, self._failed_count, self._skipped_count,
        )

    def add_activity(self, activity: AgentActivity) -> None:
        if activity.action_type == "DO_NOTHING":
            self._skipped_count += 1
            return
        self._activity_queue.put(activity)
        self._total_activities += 1
        logger.debug(
            "Aktivitaet enqueued: %s - %s", activity.agent_name, activity.action_type
        )

    def add_activity_from_dict(self, data: Dict[str, Any], platform: str) -> None:
        if "event_type" in data:
            return
        activity = AgentActivity(
            platform=platform,
            agent_id=data.get("agent_id", 0),
            agent_name=data.get("agent_name", ""),
            action_type=data.get("action_type", ""),
            action_args=data.get("action_args", {}),
            round_num=data.get("round", 0),
            timestamp=data.get("timestamp", datetime.now().isoformat()),
        )
        self.add_activity(activity)

    def _worker_loop(self, locale: str = "zh") -> None:
        set_locale(locale)
        while self._running or not self._activity_queue.empty():
            try:
                try:
                    activity = self._activity_queue.get(timeout=1)
                    platform = activity.platform.lower()
                    with self._buffer_lock:
                        self._platform_buffers.setdefault(platform, []).append(activity)
                        if len(self._platform_buffers[platform]) >= self.batch_size:
                            batch = self._platform_buffers[platform][:self.batch_size]
                            self._platform_buffers[platform] = self._platform_buffers[platform][self.batch_size:]
                            self._send_batch_activities(batch, platform)
                            time.sleep(self.send_interval)
                except Empty:
                    pass
            except Exception as e:
                logger.error("Worker-Loop-Exception: %s", e)
                time.sleep(1)

    def _send_batch_activities(
        self, activities: List[AgentActivity], platform: str
    ) -> None:
        """Insert eine Batch zusammengefasster Episoden in den LightRAG-Graphen.

        Bei LightRAG entspricht das einer vollen LLM-Extraktion ueber den
        kombinierten Text. Daher: Batch-Groesse hoch halten (mehrere Activities
        zu einem LLM-Call zusammenziehen).
        """
        if not activities:
            return
        combined = "\n".join(a.to_episode_text() for a in activities)
        for attempt in range(self.MAX_RETRIES):
            try:
                self.rag.insert(self.graph_id, combined)
                self._total_sent += 1
                self._total_items_sent += len(activities)
                display = self._get_platform_display_name(platform)
                logger.info(
                    "Batch erfolgreich: %d %s-Activities -> graph=%s",
                    len(activities), display, self.graph_id,
                )
                return
            except Exception as e:
                if attempt < self.MAX_RETRIES - 1:
                    logger.warning(
                        "Batch-Insert fehlgeschlagen (Versuch %d/%d): %s",
                        attempt + 1, self.MAX_RETRIES, e,
                    )
                    time.sleep(self.RETRY_DELAY * (attempt + 1))
                else:
                    logger.error(
                        "Batch-Insert endgueltig fehlgeschlagen nach %d Versuchen: %s",
                        self.MAX_RETRIES, e,
                    )
                    self._failed_count += 1

    def _flush_remaining(self) -> None:
        """Restliche Activities aus Queue + Buffer am Stop senden."""
        # Queue leeren -> Buffer
        while not self._activity_queue.empty():
            try:
                activity = self._activity_queue.get_nowait()
                with self._buffer_lock:
                    self._platform_buffers.setdefault(activity.platform.lower(), []).append(activity)
            except Empty:
                break
        # Buffer leeren -> Inserts (auch wenn Batch klein)
        with self._buffer_lock:
            for platform, buffer in self._platform_buffers.items():
                if buffer:
                    display = self._get_platform_display_name(platform)
                    logger.info(
                        "Flush-Restbestand: %d %s-Activities", len(buffer), display
                    )
                    self._send_batch_activities(buffer, platform)
            self._platform_buffers = {p: [] for p in self._platform_buffers}

    def get_stats(self) -> Dict[str, Any]:
        with self._buffer_lock:
            buffer_sizes = {p: len(b) for p, b in self._platform_buffers.items()}
        return {
            "graph_id": self.graph_id,
            "batch_size": self.batch_size,
            "send_interval": self.send_interval,
            "total_activities": self._total_activities,
            "batches_sent": self._total_sent,
            "items_sent": self._total_items_sent,
            "failed_count": self._failed_count,
            "skipped_count": self._skipped_count,
            "queue_size": self._activity_queue.qsize(),
            "buffer_sizes": buffer_sizes,
            "running": self._running,
        }


# ---------------------------------------------------------------------------
# Manager: ein Updater pro Simulation
# ---------------------------------------------------------------------------


class GraphMemoryManager:
    """Verwaltet pro Simulation einen GraphMemoryUpdater.

    Klassen-Methoden API identisch zu ZepGraphMemoryManager — die Caller
    in simulation_runner.py wechseln nur den Klassennamen.
    """

    _updaters: Dict[str, GraphMemoryUpdater] = {}
    _lock = threading.Lock()
    _stop_all_done = False

    @classmethod
    def create_updater(cls, simulation_id: str, graph_id: str) -> GraphMemoryUpdater:
        with cls._lock:
            existing = cls._updaters.get(simulation_id)
            if existing is not None:
                existing.stop()
            updater = GraphMemoryUpdater(graph_id)
            updater.start()
            cls._updaters[simulation_id] = updater
            logger.info(
                "GraphMemoryUpdater erzeugt: simulation=%s, graph=%s",
                simulation_id, graph_id,
            )
            return updater

    @classmethod
    def get_updater(cls, simulation_id: str) -> Optional[GraphMemoryUpdater]:
        return cls._updaters.get(simulation_id)

    @classmethod
    def stop_updater(cls, simulation_id: str) -> None:
        with cls._lock:
            updater = cls._updaters.pop(simulation_id, None)
            if updater is not None:
                updater.stop()
                logger.info("GraphMemoryUpdater gestoppt: simulation=%s", simulation_id)

    @classmethod
    def stop_all(cls) -> None:
        if cls._stop_all_done:
            return
        cls._stop_all_done = True
        with cls._lock:
            for sim_id, updater in list(cls._updaters.items()):
                try:
                    updater.stop()
                except Exception as e:
                    logger.error("Stop-Fehler simulation=%s: %s", sim_id, e)
            cls._updaters.clear()
        logger.info("Alle GraphMemoryUpdater gestoppt")

    @classmethod
    def reset_for_test(cls) -> None:
        """Test-Helper: vergisst alle Updater + setzt stop_all-Flag zurueck."""
        with cls._lock:
            cls._updaters.clear()
        cls._stop_all_done = False

    @classmethod
    def get_all_stats(cls) -> Dict[str, Dict[str, Any]]:
        return {sim_id: u.get_stats() for sim_id, u in cls._updaters.items()}


__all__ = ["AgentActivity", "GraphMemoryUpdater", "GraphMemoryManager"]
