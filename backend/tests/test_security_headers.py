"""
Tests fuer Audit-Finding M5 — Security-Headers via flask-talisman.

Validiert dass CSP, X-Frame-Options, X-Content-Type-Options,
Referrer-Policy auf jeder Antwort gesetzt sind, und dass
SECURITY_HEADERS_ENABLED=False die Header sauber entfernt.
"""

from __future__ import annotations

import pytest

from app.config import Config


@pytest.fixture
def headers_app(monkeypatch):
    """Test-App mit aktivierten Security-Headers."""
    monkeypatch.setattr(Config, "SECURITY_HEADERS_ENABLED", True, raising=False)
    monkeypatch.setattr(
        Config, "SECURITY_HEADERS_FORCE_HTTPS", False, raising=False
    )
    monkeypatch.setattr(
        Config,
        "CORS_ALLOWED_ORIGINS",
        ["http://allowed.example.com"],
        raising=False,
    )
    monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(Config, "MIROFISH_API_KEY", "x" * 32, raising=False)

    from app import create_app

    flask_app = create_app(Config)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def headers_disabled_app(monkeypatch):
    """Test-App mit deaktivierten Security-Headers."""
    monkeypatch.setattr(Config, "SECURITY_HEADERS_ENABLED", False, raising=False)
    monkeypatch.setattr(
        Config,
        "CORS_ALLOWED_ORIGINS",
        ["http://allowed.example.com"],
        raising=False,
    )
    monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(Config, "MIROFISH_API_KEY", "x" * 32, raising=False)

    from app import create_app

    flask_app = create_app(Config)
    flask_app.config["TESTING"] = True
    return flask_app


class TestSecurityHeadersEnabled:
    def test_csp_present(self, headers_app):
        client = headers_app.test_client()
        resp = client.get("/health")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src" in csp
        assert "'self'" in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    def test_x_content_type_options(self, headers_app):
        client = headers_app.test_client()
        resp = client.get("/health")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options_deny(self, headers_app):
        client = headers_app.test_client()
        resp = client.get("/health")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_referrer_policy(self, headers_app):
        client = headers_app.test_client()
        resp = client.get("/health")
        assert (
            resp.headers.get("Referrer-Policy")
            == "strict-origin-when-cross-origin"
        )

    def test_no_hsts_in_dev(self, headers_app):
        # Mit force_https=False darf HSTS nicht aktiv sein, sonst
        # zwingt der Browser localhost-Tabs in https um.
        client = headers_app.test_client()
        resp = client.get("/health")
        assert "Strict-Transport-Security" not in resp.headers


class TestSecurityHeadersDisabled:
    def test_no_csp_when_disabled(self, headers_disabled_app):
        client = headers_disabled_app.test_client()
        resp = client.get("/health")
        assert "Content-Security-Policy" not in resp.headers
