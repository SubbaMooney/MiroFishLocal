"""
Tests fuer Audit-Finding H9 — ``SELECT *`` aus User-DB ungefiltert an Client.

Vorher: ``backend/app/api/simulation.py:2029-2035,2103-2116`` lieferte rohe
``SELECT *``-Reihen direkt an den Frontend-Client. Bei OASIS-Schema-Aenderungen
(neue Columns, interne Felder) waeren die ungewollt geleakt.

Fix: Explizite Spalten-Listen ``_POST_FIELDS`` / ``_COMMENT_FIELDS`` in der
SQL-Query UND eine Response-Allow-List in ``_filter_row_to_allowlist``.
Defense-in-Depth: selbst wenn die SQL-Liste irgendwann gelockert wird,
strippt die Response-Filterung weiter.

Diese Tests verifizieren:

1. Response enthaelt nur Felder aus der Allow-List (sowohl posts als auch comments).
2. Ein zusaetzliches Feld in der Mock-DB taucht NICHT in der Response auf.
3. Die Response-Struktur ``{success, data: {posts/comments, count, total/-}}``
   bleibt unveraendert (Frontend-Vertrag).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

_TEST_KEY = "x" * 64
_SIM_ID = "sim_deadbeef12345678"


@pytest.fixture
def app_with_auth(monkeypatch):
    """Test-App mit gesetztem MIROFISH_API_KEY.

    WICHTIG: Wir importieren ``Config`` und ``create_app`` lazy in der
    Fixture, damit ein anderes Test-Modul (``test_resource_authz.py``)
    sys.modules['app*'] vorher leeren konnte ohne uns zu verstimmen —
    sonst wuerden wir ein veraltetes ``Config``-Klassenobjekt patchen.
    """
    from app.config import Config as ConfigCls

    monkeypatch.setattr(ConfigCls, "MIROFISH_API_KEY", _TEST_KEY, raising=False)
    monkeypatch.setattr(ConfigCls, "SECRET_KEY", "y" * 32, raising=False)
    monkeypatch.setattr(ConfigCls, "LLM_API_KEY", "test-key", raising=False)

    from app import create_app

    flask_app = create_app(ConfigCls)
    flask_app.config["TESTING"] = True
    flask_app.config["MIROFISH_API_KEY"] = _TEST_KEY
    return flask_app, ConfigCls


def _create_test_post_db(db_path: str, extra_columns: list[str] | None = None) -> None:
    """Erzeugt eine SQLite-DB mit ``post``-Tabelle inkl. optionaler Extra-Spalten."""
    base_cols = (
        "post_id INTEGER PRIMARY KEY",
        "user_id INTEGER",
        "original_post_id INTEGER",
        "content TEXT",
        "quote_content TEXT",
        "created_at DATETIME",
        "num_likes INTEGER DEFAULT 0",
        "num_dislikes INTEGER DEFAULT 0",
        "num_shares INTEGER DEFAULT 0",
        "num_reports INTEGER DEFAULT 0",
    )
    extra = extra_columns or []
    cols_sql = ", ".join(list(base_cols) + extra)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE post ({cols_sql})")
    insert_cols = ["post_id", "user_id", "content", "created_at",
                   "num_likes", "num_dislikes", "num_shares"]
    if extra_columns:
        for spec in extra_columns:
            col = spec.split()[0]
            insert_cols.append(col)
    placeholders = ", ".join(["?"] * len(insert_cols))
    values = [1, 42, "Hallo Welt", "2026-05-04 10:00:00", 5, 1, 2]
    if extra_columns:
        # Sentinel-Wert pro Extra-Spalte.
        for _ in extra_columns:
            values.append("LEAK_ME")
    cur.execute(
        f"INSERT INTO post ({', '.join(insert_cols)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    conn.close()


def _create_test_comment_db(db_path: str, extra_columns: list[str] | None = None) -> None:
    """Erzeugt eine SQLite-DB mit ``comment``-Tabelle inkl. optionaler Extra-Spalten."""
    base_cols = (
        "comment_id INTEGER PRIMARY KEY",
        "post_id INTEGER",
        "user_id INTEGER",
        "content TEXT",
        "created_at DATETIME",
        "num_likes INTEGER DEFAULT 0",
        "num_dislikes INTEGER DEFAULT 0",
    )
    extra = extra_columns or []
    cols_sql = ", ".join(list(base_cols) + extra)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(f"CREATE TABLE comment ({cols_sql})")
    insert_cols = ["comment_id", "post_id", "user_id", "content",
                   "created_at", "num_likes", "num_dislikes"]
    if extra_columns:
        for spec in extra_columns:
            col = spec.split()[0]
            insert_cols.append(col)
    placeholders = ", ".join(["?"] * len(insert_cols))
    values = [1, 1, 42, "Mein Kommentar", "2026-05-04 10:05:00", 3, 0]
    if extra_columns:
        for _ in extra_columns:
            values.append("LEAK_ME")
    cur.execute(
        f"INSERT INTO comment ({', '.join(insert_cols)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    conn.close()


class TestSelectStar:
    """H9: Response liefert nur whitelisted Felder, neue DB-Spalten leaken nicht."""

    def test_posts_response_only_contains_allowlisted_fields(self, app_with_auth, tmp_path, monkeypatch):
        """Response-Reihen enthalten nur Allow-List-Felder."""
        flask_app, ConfigCls = app_with_auth
        from app.api.simulation import _POST_FIELDS

        sim_dir = tmp_path / _SIM_ID
        sim_dir.mkdir()
        db_path = sim_dir / "reddit_simulation.db"
        _create_test_post_db(str(db_path))

        monkeypatch.setattr(ConfigCls, "OASIS_SIMULATION_DATA_DIR", str(tmp_path), raising=False)

        client = flask_app.test_client()
        with patch("app.utils.authz._resource_exists", return_value=True):
            resp = client.get(
                f"/api/simulation/{_SIM_ID}/posts?platform=reddit",
                headers={"X-API-Key": _TEST_KEY},
            )

        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["success"] is True
        assert payload["data"]["count"] == 1
        post = payload["data"]["posts"][0]
        # Genau die Allow-List, nichts mehr.
        assert set(post.keys()) <= set(_POST_FIELDS)
        # Keine Felder ausserhalb der Allow-List.
        for forbidden in ("original_post_id", "quote_content", "num_reports"):
            assert forbidden not in post, (
                f"Feld {forbidden!r} darf nicht im Response auftauchen"
            )

    def test_new_oasis_field_does_not_leak_into_posts_response(self, app_with_auth, tmp_path, monkeypatch):
        """Neue Spalten in der Mock-DB leaken NICHT in die Response."""
        flask_app, ConfigCls = app_with_auth
        sim_dir = tmp_path / _SIM_ID
        sim_dir.mkdir()
        db_path = sim_dir / "reddit_simulation.db"
        _create_test_post_db(str(db_path), extra_columns=[
            "internal_secret TEXT",
            "embedding_vector BLOB",
        ])

        monkeypatch.setattr(ConfigCls, "OASIS_SIMULATION_DATA_DIR", str(tmp_path), raising=False)

        client = flask_app.test_client()
        with patch("app.utils.authz._resource_exists", return_value=True):
            resp = client.get(
                f"/api/simulation/{_SIM_ID}/posts?platform=reddit",
                headers={"X-API-Key": _TEST_KEY},
            )

        assert resp.status_code == 200
        payload = resp.get_json()
        post = payload["data"]["posts"][0]
        assert "internal_secret" not in post
        assert "embedding_vector" not in post
        # Sicherheits-Sentinel darf nicht im Response stehen.
        body_text = resp.get_data(as_text=True)
        assert "LEAK_ME" not in body_text

    def test_comments_response_only_contains_allowlisted_fields(self, app_with_auth, tmp_path, monkeypatch):
        """Comments-Response enthaelt nur Allow-List-Felder."""
        flask_app, ConfigCls = app_with_auth
        from app.api.simulation import _COMMENT_FIELDS

        sim_dir = tmp_path / _SIM_ID
        sim_dir.mkdir()
        db_path = sim_dir / "reddit_simulation.db"
        _create_test_comment_db(str(db_path))

        monkeypatch.setattr(ConfigCls, "OASIS_SIMULATION_DATA_DIR", str(tmp_path), raising=False)

        client = flask_app.test_client()
        with patch("app.utils.authz._resource_exists", return_value=True):
            resp = client.get(
                f"/api/simulation/{_SIM_ID}/comments",
                headers={"X-API-Key": _TEST_KEY},
            )

        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["success"] is True
        comments = payload["data"]["comments"]
        assert len(comments) == 1
        comment = comments[0]
        assert set(comment.keys()) <= set(_COMMENT_FIELDS)

    def test_new_oasis_field_does_not_leak_into_comments_response(self, app_with_auth, tmp_path, monkeypatch):
        """Neue Spalten in der Mock-Comments-DB leaken NICHT."""
        flask_app, ConfigCls = app_with_auth
        sim_dir = tmp_path / _SIM_ID
        sim_dir.mkdir()
        db_path = sim_dir / "reddit_simulation.db"
        _create_test_comment_db(str(db_path), extra_columns=["secret_field TEXT"])

        monkeypatch.setattr(ConfigCls, "OASIS_SIMULATION_DATA_DIR", str(tmp_path), raising=False)

        client = flask_app.test_client()
        with patch("app.utils.authz._resource_exists", return_value=True):
            resp = client.get(
                f"/api/simulation/{_SIM_ID}/comments",
                headers={"X-API-Key": _TEST_KEY},
            )

        assert resp.status_code == 200
        body_text = resp.get_data(as_text=True)
        assert "LEAK_ME" not in body_text
        assert "secret_field" not in body_text

    def test_response_envelope_is_unchanged(self, app_with_auth, tmp_path, monkeypatch):
        """Frontend-Vertrag bleibt: success, data, count, total/posts/comments."""
        flask_app, ConfigCls = app_with_auth
        sim_dir = tmp_path / _SIM_ID
        sim_dir.mkdir()
        db_path = sim_dir / "reddit_simulation.db"
        _create_test_post_db(str(db_path))

        monkeypatch.setattr(ConfigCls, "OASIS_SIMULATION_DATA_DIR", str(tmp_path), raising=False)

        client = flask_app.test_client()
        with patch("app.utils.authz._resource_exists", return_value=True):
            resp = client.get(
                f"/api/simulation/{_SIM_ID}/posts?platform=reddit",
                headers={"X-API-Key": _TEST_KEY},
            )

        payload = resp.get_json()
        assert "success" in payload
        assert "data" in payload
        assert "platform" in payload["data"]
        assert "total" in payload["data"]
        assert "count" in payload["data"]
        assert "posts" in payload["data"]

    def test_filter_helper_directly(self):
        """Unit-Test fuer ``_filter_row_to_allowlist``."""
        from app.api.simulation import (
            _COMMENT_FIELDS,
            _POST_FIELDS,
            _filter_row_to_allowlist,
        )

        row = {
            "post_id": 1,
            "user_id": 2,
            "content": "ok",
            "created_at": "now",
            "num_likes": 0,
            "num_dislikes": 0,
            "num_shares": 0,
            "internal_secret": "should-not-leak",
            "embedding": b"\x00\x01",
        }
        filtered = _filter_row_to_allowlist(row, _POST_FIELDS)
        assert "internal_secret" not in filtered
        assert "embedding" not in filtered
        assert filtered["content"] == "ok"

        # Leere Zeile -> leeres Dict.
        assert _filter_row_to_allowlist({}, _POST_FIELDS) == {}
        # Zeile ohne Allow-List-Felder -> leeres Dict.
        assert _filter_row_to_allowlist(
            {"unrelated": 1}, _POST_FIELDS
        ) == {}
