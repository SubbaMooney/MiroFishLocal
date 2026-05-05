"""
Tests fuer Token-Tracker und /api/admin/tokens-Endpoint.
"""

from __future__ import annotations

import pytest

from app.utils.token_tracker import TokenTracker, _resolve_price


class TestTrackerCore:
    def test_record_basic(self):
        t = TokenTracker()
        t.record("gpt-4o-mini", 1000, 500, "test")
        snap = t.snapshot()
        assert snap["totals"]["calls"] == 1
        assert snap["totals"]["prompt_tokens"] == 1000
        assert snap["totals"]["completion_tokens"] == 500
        assert snap["totals"]["total_tokens"] == 1500
        # 1000 * 0.15/1M + 500 * 0.60/1M = 0.00015 + 0.0003 = 0.00045
        assert snap["totals"]["cost_usd"] == pytest.approx(0.00045)

    def test_aggregate_by_purpose(self):
        t = TokenTracker()
        t.record("gpt-4o-mini", 100, 50, "extract")
        t.record("gpt-4o-mini", 200, 75, "extract")
        t.record("gpt-4o-mini", 300, 100, "summary")
        snap = t.snapshot()
        m = snap["by_model"][0]
        assert m["calls"] == 3
        purposes = {p["purpose"]: p for p in m["by_purpose"]}
        assert purposes["extract"]["calls"] == 2
        assert purposes["extract"]["prompt_tokens"] == 300
        assert purposes["summary"]["calls"] == 1

    def test_zero_tokens_not_recorded(self):
        t = TokenTracker()
        t.record("gpt-4o-mini", 0, 0, "noop")
        assert t.snapshot()["totals"]["calls"] == 0

    def test_unknown_model_zero_cost(self):
        t = TokenTracker()
        t.record("custom-llama-7b", 1000, 500, "test")
        snap = t.snapshot()
        assert snap["totals"]["cost_usd"] == 0.0

    def test_fuzzy_model_match(self):
        # Versionierte Modellnamen sollen auf Familie matchen.
        price = _resolve_price("gpt-4o-mini-2024-07-18")
        assert price["input"] == 0.15
        assert price["output"] == 0.60

    def test_reset(self):
        t = TokenTracker()
        t.record("gpt-4o-mini", 100, 50, "x")
        t.reset()
        assert t.snapshot()["totals"]["calls"] == 0


class TestEmbeddingTracking:
    def test_embedding_input_only(self):
        t = TokenTracker()
        t.record("text-embedding-3-small", 10000, 0, "lightrag:embed")
        snap = t.snapshot()
        # 10000 * 0.02 / 1M = 0.0002
        assert snap["totals"]["cost_usd"] == pytest.approx(0.0002)
        assert snap["totals"]["completion_tokens"] == 0


class TestAdminEndpoint:
    @pytest.fixture
    def client(self, monkeypatch):
        from app.config import Config

        monkeypatch.setattr(Config, "MIROFISH_API_KEY", "x" * 40, raising=False)
        monkeypatch.setattr(Config, "SECRET_KEY", "y" * 32, raising=False)
        monkeypatch.setattr(Config, "LLM_API_KEY", "fake", raising=False)
        monkeypatch.setattr(
            Config, "CORS_ALLOWED_ORIGINS",
            ["http://localhost:3000"], raising=False,
        )
        monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", False, raising=False)
        monkeypatch.setattr(Config, "SECURITY_HEADERS_ENABLED", False, raising=False)

        from app import create_app
        from app.utils.token_tracker import tracker as global_tracker
        global_tracker.reset()

        app = create_app(Config)
        app.config["TESTING"] = True
        return app.test_client()

    def _hdr(self):
        return {"X-API-Key": "x" * 40}

    def test_get_tokens_unauth(self, client):
        resp = client.get("/api/admin/tokens")
        assert resp.status_code == 401

    def test_get_tokens_empty(self, client):
        resp = client.get("/api/admin/tokens", headers=self._hdr())
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["data"]["totals"]["calls"] == 0

    def test_get_tokens_after_record(self, client):
        from app.utils.token_tracker import tracker as global_tracker
        global_tracker.record("gpt-4o-mini", 1000, 500, "persona:gen")
        resp = client.get("/api/admin/tokens", headers=self._hdr())
        body = resp.get_json()
        assert body["data"]["totals"]["calls"] == 1
        assert body["data"]["totals"]["prompt_tokens"] == 1000
        assert body["data"]["by_model"][0]["model"] == "gpt-4o-mini"

    def test_reset_endpoint(self, client):
        from app.utils.token_tracker import tracker as global_tracker
        global_tracker.record("gpt-4o-mini", 100, 50, "x")
        resp = client.post("/api/admin/tokens/reset", headers=self._hdr())
        assert resp.status_code == 200
        snap = client.get("/api/admin/tokens", headers=self._hdr()).get_json()
        assert snap["data"]["totals"]["calls"] == 0
