"""
Phase-4.5 Tests: Cost-Optimization Knobs in lightrag_factory.

Validiert, dass die in Config.LIGHTRAG_* gesetzten Knobs an die LightRAG-
Instanz durchgereicht werden, und dass der Examples-Drop idempotent ist.

Reine Unit-Tests; spaert echtes LightRAG.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Config-Defaults
# ---------------------------------------------------------------------------


def test_config_defaults_match_optimized_values():
    """Defaults sollen kosten-optimiert sein, nicht LightRAG-Original-Defaults."""
    from app.config import Config

    assert Config.LIGHTRAG_CHUNK_TOKEN_SIZE == 5000  # vs LightRAG default 1200
    assert Config.LIGHTRAG_CHUNK_OVERLAP_TOKEN_SIZE == 200  # vs LightRAG default 100
    assert Config.LIGHTRAG_MAX_GLEANING == 0  # Single-Pass; LightRAG default 1
    assert Config.LIGHTRAG_MAX_EXTRACT_INPUT_TOKENS == 8000
    assert Config.LIGHTRAG_DROP_EXAMPLES is True
    assert Config.DEFAULT_CHUNK_SIZE == 5000  # MiroFish-side, war frueher 500


# Hinweis: Es gibt KEINEN Test fuer Env-Var-Override via importlib.reload.
# Class-Level os.environ.get Reads sind Python-Stdlib-Verhalten — Module-
# Reload zur Test-Zeit verursacht State-Leak in lightrag_factory's Config-
# Referenz. Vertraue der Stdlib; die Defaults werden oben getestet, ein
# einzelner Override-Pfad ist via monkeypatch.setattr auf Config selbst
# bequemer (siehe test_create_rag_overridable_via_config).


# ---------------------------------------------------------------------------
# create_rag: Cost-Knobs werden durchgereicht
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_lightrag(monkeypatch):
    """Patcht lightrag.LightRAG so, dass wir die Konstruktor-Kwargs einfangen."""
    captured = {}

    class FakeLightRAG:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.kwargs = kwargs

        async def initialize_storages(self):
            pass

    async def fake_init_pipeline_status():
        pass

    # Verschiedene Patch-Ziele: import-time vs lazy
    import lightrag
    import lightrag.kg.shared_storage

    monkeypatch.setattr(lightrag, "LightRAG", FakeLightRAG)
    monkeypatch.setattr(
        lightrag.kg.shared_storage, "initialize_pipeline_status", fake_init_pipeline_status
    )

    # Stub die LLM/Embed-Builder (brauchen sonst echte API-Keys)
    from app.services import lightrag_factory

    monkeypatch.setattr(lightrag_factory, "create_llm_func", lambda **kw: lambda *a, **k: None)
    monkeypatch.setattr(lightrag_factory, "create_embed_func", lambda **kw: object())

    return captured


def test_create_rag_passes_cost_knobs(fake_lightrag, tmp_path):
    """LightRAG() bekommt chunk_token_size, gleaning, etc. aus Config."""
    import asyncio

    from app.services import lightrag_factory

    asyncio.run(lightrag_factory.create_rag(str(tmp_path)))

    assert fake_lightrag["chunk_token_size"] == 5000
    assert fake_lightrag["chunk_overlap_token_size"] == 200
    assert fake_lightrag["entity_extract_max_gleaning"] == 0
    assert fake_lightrag["max_extract_input_tokens"] == 8000
    assert fake_lightrag["working_dir"] == str(tmp_path)


def test_create_rag_overridable_via_config(fake_lightrag, tmp_path, monkeypatch):
    """Bei Config-Aenderung muessen die neuen Werte durchgereicht werden."""
    import asyncio

    from app.config import Config
    from app.services import lightrag_factory

    monkeypatch.setattr(Config, "LIGHTRAG_CHUNK_TOKEN_SIZE", 1200, raising=False)
    monkeypatch.setattr(Config, "LIGHTRAG_MAX_GLEANING", 2, raising=False)

    asyncio.run(lightrag_factory.create_rag(str(tmp_path)))

    assert fake_lightrag["chunk_token_size"] == 1200
    assert fake_lightrag["entity_extract_max_gleaning"] == 2


# ---------------------------------------------------------------------------
# Examples-Drop: idempotent + reagiert auf Config
# ---------------------------------------------------------------------------


def test_drop_examples_when_enabled(monkeypatch):
    """Bei aktiviertem DROP_EXAMPLES wird PROMPTS["entity_extraction_examples"] geleert."""
    from app.config import Config
    from app.services import lightrag_factory
    from lightrag.prompt import PROMPTS

    # State zuruecksetzen — Tests sollen unabhaengig laufen
    original = PROMPTS.get("entity_extraction_examples", [])
    monkeypatch.setattr(lightrag_factory, "_PROMPTS_PATCHED", False, raising=False)
    monkeypatch.setattr(Config, "LIGHTRAG_DROP_EXAMPLES", True, raising=False)

    try:
        # Setze einen Marker-Wert, damit wir Aenderung beobachten
        PROMPTS["entity_extraction_examples"] = ["DUMMY-EXAMPLE"]

        lightrag_factory._apply_prompts_optimization()

        assert PROMPTS["entity_extraction_examples"] == []
        assert lightrag_factory._PROMPTS_PATCHED is True
    finally:
        PROMPTS["entity_extraction_examples"] = original
        lightrag_factory._PROMPTS_PATCHED = False


def test_drop_examples_when_disabled(monkeypatch):
    """Bei deaktiviertem DROP_EXAMPLES bleibt PROMPTS unveraendert."""
    from app.config import Config
    from app.services import lightrag_factory
    from lightrag.prompt import PROMPTS

    original = PROMPTS.get("entity_extraction_examples", [])
    monkeypatch.setattr(lightrag_factory, "_PROMPTS_PATCHED", False, raising=False)
    monkeypatch.setattr(Config, "LIGHTRAG_DROP_EXAMPLES", False, raising=False)

    try:
        PROMPTS["entity_extraction_examples"] = ["KEEP-ME"]
        lightrag_factory._apply_prompts_optimization()
        assert PROMPTS["entity_extraction_examples"] == ["KEEP-ME"]
        assert lightrag_factory._PROMPTS_PATCHED is False
    finally:
        PROMPTS["entity_extraction_examples"] = original
        lightrag_factory._PROMPTS_PATCHED = False


def test_drop_examples_idempotent(monkeypatch):
    """Mehrfacher Aufruf darf den State nicht ueberschreiben (z.B. wenn
    der User PROMPTS zwischenzeitlich manuell editiert hat)."""
    from app.config import Config
    from app.services import lightrag_factory
    from lightrag.prompt import PROMPTS

    original = PROMPTS.get("entity_extraction_examples", [])
    monkeypatch.setattr(lightrag_factory, "_PROMPTS_PATCHED", False, raising=False)
    monkeypatch.setattr(Config, "LIGHTRAG_DROP_EXAMPLES", True, raising=False)

    try:
        PROMPTS["entity_extraction_examples"] = ["EX1", "EX2", "EX3"]
        lightrag_factory._apply_prompts_optimization()
        assert PROMPTS["entity_extraction_examples"] == []

        # User editiert PROMPTS manuell (z.B. fuer Quality-Test)
        PROMPTS["entity_extraction_examples"] = ["RE-ADDED"]
        lightrag_factory._apply_prompts_optimization()  # 2. Call
        # Idempotent: 2. Call darf nicht erneut leeren
        assert PROMPTS["entity_extraction_examples"] == ["RE-ADDED"]
    finally:
        PROMPTS["entity_extraction_examples"] = original
        lightrag_factory._PROMPTS_PATCHED = False
