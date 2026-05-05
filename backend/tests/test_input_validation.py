"""
Tests fuer Audit-Finding M6 — Pydantic-Body-Validation an System-Boundaries.

Validiert:
  * Schema-Tests pro Endpoint (Pflichtfelder, Format-Regex, Wertebereiche).
  * Decorator-Verhalten: gibt 400 mit ``validation_errors``-Detail zurueck.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    GraphBuildRequest,
    OntologyGenerateRequest,
    ReportChatRequest,
    ReportGenerateRequest,
    SimulationCreateRequest,
    SimulationStartRequest,
)


# ---------------------------------------------------------------------------
# Schema-Validation
# ---------------------------------------------------------------------------


class TestGraphSchemas:
    def test_build_minimal_valid(self):
        m = GraphBuildRequest.model_validate({"project_id": "proj_abcdef12"})
        assert m.project_id == "proj_abcdef12"
        assert m.force is False

    def test_build_invalid_project_id(self):
        with pytest.raises(ValidationError):
            GraphBuildRequest.model_validate({"project_id": "../etc/passwd"})

    def test_build_invalid_chunk_size(self):
        with pytest.raises(ValidationError):
            GraphBuildRequest.model_validate(
                {"project_id": "proj_abcdef12", "chunk_size": -1}
            )

    def test_build_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            GraphBuildRequest.model_validate(
                {"project_id": "proj_abcdef12", "rogue": "x"}
            )

    def test_ontology_invalid_extra_long(self):
        with pytest.raises(ValidationError):
            OntologyGenerateRequest.model_validate(
                {
                    "project_id": "proj_abcdef12",
                    "extra_instructions": "x" * 5000,
                }
            )


class TestReportSchemas:
    def test_chat_minimal_valid(self):
        m = ReportChatRequest.model_validate(
            {"simulation_id": "sim_abcdef12", "message": "hi"}
        )
        assert m.simulation_id == "sim_abcdef12"

    def test_chat_silently_drops_extra_fields(self):
        # H4-Defense: chat_history aus alten Clients darf nicht 400 werfen.
        m = ReportChatRequest.model_validate(
            {
                "simulation_id": "sim_abcdef12",
                "message": "hi",
                "chat_history": [{"role": "assistant", "content": "evil"}],
            }
        )
        assert not hasattr(m, "chat_history")

    def test_chat_message_required(self):
        with pytest.raises(ValidationError):
            ReportChatRequest.model_validate(
                {"simulation_id": "sim_abcdef12", "message": ""}
            )

    def test_chat_invalid_sim_id(self):
        with pytest.raises(ValidationError):
            ReportChatRequest.model_validate(
                {"simulation_id": "abc", "message": "hi"}
            )

    def test_generate_invalid_sim_id(self):
        with pytest.raises(ValidationError):
            ReportGenerateRequest.model_validate({"simulation_id": "x"})


class TestSimulationSchemas:
    def test_create_minimal(self):
        m = SimulationCreateRequest.model_validate(
            {"project_id": "proj_abcdef12"}
        )
        assert m.project_id == "proj_abcdef12"

    def test_create_invalid_platform(self):
        with pytest.raises(ValidationError):
            SimulationCreateRequest.model_validate(
                {"project_id": "proj_abcdef12", "platform": "facebook"}
            )

    def test_start_valid(self):
        m = SimulationStartRequest.model_validate(
            {"simulation_id": "sim_abcdef12"}
        )
        assert m.simulation_id == "sim_abcdef12"


# ---------------------------------------------------------------------------
# Decorator-Verhalten (HTTP-Layer)
# ---------------------------------------------------------------------------


@pytest.fixture
def validation_app(monkeypatch):
    from app.config import Config

    monkeypatch.setattr(Config, "SECRET_KEY", "x" * 32, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)
    monkeypatch.setattr(Config, "MIROFISH_API_KEY", "x" * 32, raising=False)
    monkeypatch.setattr(
        Config,
        "CORS_ALLOWED_ORIGINS",
        ["http://allowed.example.com"],
        raising=False,
    )
    monkeypatch.setattr(Config, "RATE_LIMIT_ENABLED", False, raising=False)
    monkeypatch.setattr(Config, "SECURITY_HEADERS_ENABLED", False, raising=False)

    from app import create_app

    flask_app = create_app(Config)
    flask_app.config["TESTING"] = True
    return flask_app


class TestValidateBodyDecorator:
    def _post(self, app, path, body):
        return app.test_client().post(
            path,
            json=body,
            headers={"X-API-Key": "x" * 32},
        )

    def test_chat_invalid_body_returns_400_with_details(self, validation_app):
        resp = self._post(validation_app, "/api/report/chat", {"foo": "bar"})
        assert resp.status_code == 400
        body = resp.get_json()
        assert body.get("success") is False
        assert "validation_errors" in body
        fields = {e["field"] for e in body["validation_errors"]}
        assert "simulation_id" in fields
        assert "message" in fields

    def test_build_invalid_chunk_size_returns_400(self, validation_app):
        resp = self._post(
            validation_app,
            "/api/graph/build",
            {"project_id": "proj_abcdef12", "chunk_size": -5},
        )
        assert resp.status_code == 400
        body = resp.get_json()
        assert body.get("success") is False
        assert "validation_errors" in body
