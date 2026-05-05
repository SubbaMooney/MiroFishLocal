"""
Tests fuer den Resource-Ownership-Decorator (Audit-Finding H3).

Schwerpunkt:

- Syntaktisch valide aber nicht registrierte IDs liefern ``404`` und keine
  Daten / keine Aktion auslosen.
- Registrierte IDs durchlaufen den Handler normal.
- Ungueltiges ID-Format wird ebenfalls ``404`` (nicht ``400``), damit der
  Server nicht den Unterschied zwischen "format ungueltig" und
  "nicht gefunden" preisgibt.
- Kein Fall-Through-Pfad: bei nicht registrierter ID darf der eigentliche
  Handler nicht laufen (kein ``Zep``-Call, kein ``rmtree``).

Fixture-Strategie: ``tmp_path`` mit ``Config.UPLOAD_FOLDER``-Override,
plus ``X-API-Key`` Header (C1-Auth ist seit ``dc85ea3`` aktiv) und
``MIROFISH_API_KEY`` ENV. Damit laufen die Tests gegen einen echten
Flask-Test-Client.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Repo-Root in sys.path haengen, damit ``import app`` funktioniert.
_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Frisch konfigurierte Flask-App mit isoliertem ``UPLOAD_FOLDER``."""
    monkeypatch.setenv('MIROFISH_API_KEY', 'x' * 40)
    monkeypatch.setenv('LLM_API_KEY', 'fake')
    monkeypatch.setenv('LLM_BASE_URL', 'http://example.invalid')
    monkeypatch.setenv('LLM_MODEL_NAME', 'fake-model')
    monkeypatch.setenv('ZEP_API_KEY', 'fake')
    monkeypatch.setenv('CORS_ALLOWED_ORIGINS', 'http://localhost:3000')

    # UPLOAD_FOLDER muss vor dem App-Import gesetzt sein, weil die
    # Manager-Klassen ihre Storage-Pfade beim Klassen-Load ableiten.
    monkeypatch.setenv('UPLOAD_FOLDER', str(tmp_path / 'uploads'))

    # Etwaige geladene App-Module verwerfen, damit ``UPLOAD_FOLDER``
    # neu gelesen wird.
    for mod in list(sys.modules.keys()):
        if mod.startswith('app'):
            del sys.modules[mod]

    from app import create_app
    from app.config import Config

    # Config laedt .env mit override=True und ueberschreibt damit alle
    # monkeypatch.setenv-Werte. Wir patchen daher Config-Klassen-Vars
    # direkt nach dem Import — analog zu den anderen Test-Modulen
    # (test_cors.py, test_input_validation.py etc.).
    monkeypatch.setattr(Config, 'MIROFISH_API_KEY', 'x' * 40, raising=False)
    monkeypatch.setattr(Config, 'LLM_API_KEY', 'fake', raising=False)
    monkeypatch.setattr(Config, 'CORS_ALLOWED_ORIGINS',
                        ['http://localhost:3000'], raising=False)
    monkeypatch.setattr(Config, 'SECRET_KEY', 'y' * 32, raising=False)

    # Storage-Roots auf den tmp-Pfad zwingen.
    Config.UPLOAD_FOLDER = str(tmp_path / 'uploads')
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

    # Manager-Klassen lesen ihre Pfade aus Config beim Class-Load.
    # Wir patchen die Klassen-Variablen direkt.
    from app.models.project import ProjectManager
    from app.services.report_agent import ReportManager
    from app.services.simulation_manager import SimulationManager

    ProjectManager.PROJECTS_DIR = os.path.join(Config.UPLOAD_FOLDER, 'projects')
    ReportManager.REPORTS_DIR = os.path.join(Config.UPLOAD_FOLDER, 'reports')
    SimulationManager.SIMULATION_DATA_DIR = os.path.join(Config.UPLOAD_FOLDER, 'simulations')

    app = create_app()
    app.config['TESTING'] = True
    client = app.test_client()
    return client, app


def _auth_headers():
    return {'X-API-Key': 'x' * 40}


# --------------------------------------------------------------------------
# project resource
# --------------------------------------------------------------------------

class TestProjectScope:
    def test_unregistered_valid_id_returns_404(self, app_client):
        client, _ = app_client
        # Format ist gueltig (proj_<hex>), aber nicht in Registry.
        resp = client.get('/api/graph/project/proj_deadbeefcafe', headers=_auth_headers())
        assert resp.status_code == 404

    def test_invalid_format_returns_404(self, app_client):
        client, _ = app_client
        resp = client.get('/api/graph/project/../etc/passwd', headers=_auth_headers())
        # Nicht 400 -- generisches 404.
        assert resp.status_code == 404

    def test_registered_id_passes(self, app_client):
        client, _ = app_client
        from app.models.project import ProjectManager
        project = ProjectManager.create_project(name='test')
        try:
            resp = client.get(
                f'/api/graph/project/{project.project_id}',
                headers=_auth_headers(),
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data['success'] is True
            assert data['data']['project_id'] == project.project_id
        finally:
            ProjectManager.delete_project(project.project_id)

    def test_delete_unregistered_id_returns_404_no_action(self, app_client):
        client, _ = app_client
        from app.models.project import ProjectManager
        # Vorher: keine Projekte.
        before = ProjectManager.list_projects(limit=1000)
        resp = client.delete(
            '/api/graph/project/proj_aaaabbbbcccc',
            headers=_auth_headers(),
        )
        assert resp.status_code == 404
        # Nachher: gleiche Projektliste, also kein Side-Effect.
        after = ProjectManager.list_projects(limit=1000)
        assert {p.project_id for p in before} == {p.project_id for p in after}


# --------------------------------------------------------------------------
# report resource
# --------------------------------------------------------------------------

class TestReportScope:
    def test_unregistered_valid_id_returns_404(self, app_client):
        client, _ = app_client
        resp = client.get('/api/report/report_deadbeefcafe', headers=_auth_headers())
        assert resp.status_code == 404

    def test_invalid_format_returns_404(self, app_client):
        client, _ = app_client
        resp = client.get('/api/report/proj_deadbeefcafe', headers=_auth_headers())
        # Falscher Praefix -> safe_id wirft, Decorator antwortet 404.
        assert resp.status_code == 404


# --------------------------------------------------------------------------
# graph resource (free-format ids, not safe_id)
# --------------------------------------------------------------------------

class TestGraphScope:
    def test_unregistered_graph_id_returns_404(self, app_client):
        client, _ = app_client
        resp = client.get('/api/graph/data/mirofish_unknown', headers=_auth_headers())
        assert resp.status_code == 404

    def test_invalid_graph_id_format_returns_404(self, app_client):
        client, _ = app_client
        # Slashes (Pfad-Separator) sind nicht erlaubt; Flask routed das eh
        # nicht, aber URL-encoded landet's auf der Route.
        resp = client.get(
            '/api/graph/data/has%20space',
            headers=_auth_headers(),
        )
        assert resp.status_code == 404

    def test_registered_graph_id_passes_decorator(self, app_client, monkeypatch):
        """Ein in einem Projekt registriertes ``graph_id`` darf den
        Decorator passieren (was *danach* passiert -- z. B. Zep-Aufruf --
        ist hier nicht im Test-Scope)."""
        client, _ = app_client
        from app.models.project import ProjectManager
        from app.api import graph as graph_api

        project = ProjectManager.create_project(name='gtest')
        project.graph_id = 'mirofish_registered_xyz'
        ProjectManager.save_project(project)

        # Zep-Call stub: wenn der Decorator passt, ruft die Route
        # GraphBuilderService.get_graph_data auf. Wir patchen das im
        # ``graph_api``-Modul-Namespace, weil die Route den Symbol-Lookup
        # dort macht (``from ..services.graph_builder import GraphBuilderService``).
        called = {}

        class _Stub:
            def get_graph_data(self, graph_id):
                called['gid'] = graph_id
                return {'nodes': [], 'edges': []}

        monkeypatch.setattr(graph_api, 'GraphBuilderService', _Stub)

        try:
            resp = client.get(
                '/api/graph/data/mirofish_registered_xyz',
                headers=_auth_headers(),
            )
            assert resp.status_code == 200
            assert called.get('gid') == 'mirofish_registered_xyz'
        finally:
            ProjectManager.delete_project(project.project_id)


# --------------------------------------------------------------------------
# simulation resource
# --------------------------------------------------------------------------

class TestSimulationScope:
    def test_unregistered_id_returns_404(self, app_client):
        client, _ = app_client
        resp = client.get('/api/simulation/sim_deadbeefcafe', headers=_auth_headers())
        assert resp.status_code == 404

    def test_invalid_format_returns_404(self, app_client):
        client, _ = app_client
        resp = client.get(
            '/api/simulation/not-a-valid-id',
            headers=_auth_headers(),
        )
        assert resp.status_code == 404

    def test_posts_unregistered_id_returns_404(self, app_client):
        client, _ = app_client
        resp = client.get(
            '/api/simulation/sim_aaaabbbbcccc/posts',
            headers=_auth_headers(),
        )
        assert resp.status_code == 404


# --------------------------------------------------------------------------
# Decorator unit tests (no Flask)
# --------------------------------------------------------------------------

class TestDecoratorUnit:
    def test_unknown_kind_raises(self):
        from app.utils.authz import require_resource
        with pytest.raises(ValueError):
            require_resource('user', 'user_id')

    def test_graph_id_format_regex(self):
        """Liberales Format fuer LightRAG-IDs."""
        from app.utils.authz import _GRAPH_ID_RE
        assert _GRAPH_ID_RE.match('mirofish_abc123')
        assert _GRAPH_ID_RE.match('a-b_c')
        assert not _GRAPH_ID_RE.match('has space')
        assert not _GRAPH_ID_RE.match('../etc/passwd')
        assert not _GRAPH_ID_RE.match('')
        assert not _GRAPH_ID_RE.match('a' * 200)
