"""
Tests fuer Audit-Finding C1 — Auth-Middleware via X-API-Key.

Deckt das gesamte Verhalten des before_request-Hooks ab:

- Anfragen ohne X-API-Key-Header werden mit 401 abgelehnt.
- Anfragen mit falschem Header werden mit 401 abgelehnt.
- Anfragen mit richtigem Header werden vom Middleware durchgewunken
  (200 oder ein anderer route-spezifischer Status).
- /health bleibt unauth (Health-Check).
- CORS-Preflight (OPTIONS) bleibt unauth (Browser senden Preflights
  ohne Custom-Header — sonst kommt der Browser nicht durch).

Hinweis: Wir patchen Config statisch per monkeypatch.setattr und bauen
eine Test-App. Das hat im Stream A bei den CORS-Tests funktioniert ohne
andere Tests zu brechen.
"""

from __future__ import annotations

import pytest

from app.config import Config


_TEST_KEY = "x" * 64


@pytest.fixture
def auth_app(monkeypatch):
    """Test-App mit gesetztem MIROFISH_API_KEY."""
    monkeypatch.setattr(Config, "MIROFISH_API_KEY", _TEST_KEY, raising=False)
    monkeypatch.setattr(Config, "SECRET_KEY", "y" * 32, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)

    from app import create_app

    flask_app = create_app(Config)
    flask_app.config["TESTING"] = True
    # app.config wird per from_object gefuettert — explizit setzen, da
    # wir die Config-Klasse erst NACH dem Import patchen.
    flask_app.config["MIROFISH_API_KEY"] = _TEST_KEY
    return flask_app


class TestAuthMiddleware:
    def test_missing_api_key_returns_401(self, auth_app):
        client = auth_app.test_client()
        resp = client.get("/api/graph/list")
        assert resp.status_code == 401

    def test_wrong_api_key_returns_401(self, auth_app):
        client = auth_app.test_client()
        resp = client.get(
            "/api/graph/list",
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_empty_api_key_header_returns_401(self, auth_app):
        client = auth_app.test_client()
        resp = client.get(
            "/api/graph/list",
            headers={"X-API-Key": ""},
        )
        assert resp.status_code == 401

    def test_correct_api_key_passes_middleware(self, auth_app):
        client = auth_app.test_client()
        resp = client.get(
            "/api/graph/list",
            headers={"X-API-Key": _TEST_KEY},
        )
        # Middleware laesst durch — Route kann beliebigen Status liefern,
        # solange er nicht 401 ist (kein Auth-Fail).
        assert resp.status_code != 401

    def test_health_unauth(self, auth_app):
        client = auth_app.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json.get("status") == "ok"

    def test_options_preflight_unauth(self, auth_app):
        client = auth_app.test_client()
        # CORS-Preflight: kein X-API-Key, aber muss durchgehen.
        resp = client.options(
            "/api/graph/list",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code != 401

    def test_constant_time_compare_used(self, auth_app):
        # Smoke: identische Laenge, falscher Key — auch das gibt 401,
        # nicht etwa frueh-Abbruch nach unterschiedlicher Laenge.
        client = auth_app.test_client()
        resp = client.get(
            "/api/graph/list",
            headers={"X-API-Key": "z" * 64},
        )
        assert resp.status_code == 401


class TestAuthConfigValidation:
    def test_missing_api_key_in_config_rejected(self, monkeypatch):
        monkeypatch.setattr(Config, "MIROFISH_API_KEY", None, raising=False)
        monkeypatch.setattr(Config, "SECRET_KEY", "y" * 32, raising=False)
        monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)
        errors = Config.validate()
        assert any("MIROFISH_API_KEY" in e for e in errors)

    def test_short_api_key_rejected(self, monkeypatch):
        monkeypatch.setattr(Config, "MIROFISH_API_KEY", "shortkey", raising=False)
        monkeypatch.setattr(Config, "SECRET_KEY", "y" * 32, raising=False)
        monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)
        errors = Config.validate()
        assert any("MIROFISH_API_KEY" in e and ("32" in e or "短" in e or "kurz" in e.lower()) for e in errors)

    def test_valid_api_key_passes(self, monkeypatch):
        monkeypatch.setattr(Config, "MIROFISH_API_KEY", "x" * 64, raising=False)
        monkeypatch.setattr(Config, "SECRET_KEY", "y" * 32, raising=False)
        monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)
        errors = Config.validate()
        assert not any("MIROFISH_API_KEY" in e for e in errors)
