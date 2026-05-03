"""
Tests fuer Audit-Finding C3 — CORS-Whitelist.

Validiert die Config-Validation (lehnt '*' ab, fordert mindestens eine
Origin) und das Flask-CORS-Wiring (whitelisted Origin bekommt Header,
andere nicht).

Hinweis: ``app.config`` liest ENV beim Modul-Load. Statt das Modul zu
reloaden (was andere Test-Module bricht), patchen wir die ``Config``-
Klasse direkt fuer die Validate-Tests und legen fuer das Wiring-Test
einen Mini-Config-Klon mit eigener CORS-Whitelist an.
"""

from __future__ import annotations

import pytest

from app.config import Config


# ---------------------------------------------------------------------------
# Config-Validation
# ---------------------------------------------------------------------------


class TestCorsConfigValidation:
    def _patched_config(self, monkeypatch, origins):
        monkeypatch.setattr(Config, "CORS_ALLOWED_ORIGINS", origins, raising=False)
        # SECRET_KEY/LLM_API_KEY muessen wahr sein, damit andere
        # Validation-Fehler den CORS-Test nicht maskieren.
        monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32, raising=False)
        monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)

    def test_wildcard_rejected(self, monkeypatch):
        self._patched_config(monkeypatch, ["*"])
        errors = Config.validate()
        assert any("CORS_ALLOWED_ORIGINS" in e and "*" in e for e in errors)

    def test_empty_rejected(self, monkeypatch):
        self._patched_config(monkeypatch, [])
        errors = Config.validate()
        assert any("CORS_ALLOWED_ORIGINS" in e for e in errors)

    def test_explicit_origins_pass(self, monkeypatch):
        self._patched_config(
            monkeypatch,
            ["http://localhost:3000", "http://example.com"],
        )
        errors = Config.validate()
        assert not any("CORS" in e for e in errors)

    def test_default_excludes_wildcard(self):
        # Out of the box (kein ENV-Override) darf '*' nicht enthalten sein.
        assert "*" not in Config.CORS_ALLOWED_ORIGINS
        assert len(Config.CORS_ALLOWED_ORIGINS) >= 1


# ---------------------------------------------------------------------------
# Flask-CORS-Wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def cors_app(monkeypatch):
    """Test-App mit kontrollierter CORS-Whitelist.

    Wir bauen eine Mini-Config-Klasse und patchen Config.CORS_ALLOWED_ORIGINS
    statisch — kein Modul-Reload, damit andere Test-Module unberuehrt bleiben.
    """
    monkeypatch.setattr(
        Config,
        "CORS_ALLOWED_ORIGINS",
        ["http://allowed.example.com"],
        raising=False,
    )
    monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)

    from app import create_app

    flask_app = create_app(Config)
    flask_app.config["TESTING"] = True
    return flask_app


class TestCorsWiring:
    def test_allowed_origin_gets_cors_header(self, cors_app):
        client = cors_app.test_client()
        resp = client.options(
            "/api/graph/list",
            headers={
                "Origin": "http://allowed.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert (
            resp.headers.get("Access-Control-Allow-Origin")
            == "http://allowed.example.com"
        )

    def test_disallowed_origin_no_cors_header(self, cors_app):
        client = cors_app.test_client()
        resp = client.options(
            "/api/graph/list",
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert (
            resp.headers.get("Access-Control-Allow-Origin")
            != "http://evil.example.com"
        )

    def test_health_endpoint_works(self, cors_app):
        client = cors_app.test_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json.get("status") == "ok"
