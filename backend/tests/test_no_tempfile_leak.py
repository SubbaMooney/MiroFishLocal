"""
Tests fuer Audit-Finding H8 — Tempfile-Leak im Report-Download.

Vorher: `backend/app/api/report.py:417-427` legte `NamedTemporaryFile(delete=False)`
an und liess es ungeloescht in /tmp liegen. Bei jedem Download ohne persistierte
MD-Datei wuchs `/tmp` und sensible Inhalte verblieben dort.

Fix (siehe report.py): In-Memory-Content wird direkt als `Response` mit
`text/markdown`-Mimetype und `Content-Disposition: attachment` ausgeliefert.
Wenn die persistente MD-Datei existiert, wird `send_file` weiterhin verwendet,
aber explizit mit Mimetype.

Diese Tests verifizieren:

1. Download liefert 200 + Markdown-Body, wenn nur In-Memory-Content vorliegt.
2. /tmp waechst nicht (Vergleich Datei-Anzahl vor/nach Request).
3. `Content-Disposition` enthaelt den korrekten Dateinamen.
4. Keine `NamedTemporaryFile(delete=False)`-Aufrufe mehr im Backend-Code.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from app.config import Config


_TEST_KEY = "x" * 64


@pytest.fixture
def app_with_auth(monkeypatch):
    """Test-App mit gesetztem MIROFISH_API_KEY."""
    monkeypatch.setattr(Config, "MIROFISH_API_KEY", _TEST_KEY, raising=False)
    monkeypatch.setattr(Config, "SECRET_KEY", "y" * 32, raising=False)
    monkeypatch.setattr(Config, "LLM_API_KEY", "test-key", raising=False)

    from app import create_app

    flask_app = create_app(Config)
    flask_app.config["TESTING"] = True
    flask_app.config["MIROFISH_API_KEY"] = _TEST_KEY
    return flask_app


def _count_tmp_files() -> int:
    """Zaehlt Dateien in /tmp (oder dem System-Tempdir)."""
    tmp_dir = Path(tempfile.gettempdir())
    return sum(1 for _ in tmp_dir.iterdir())


class TestNoTempfileLeak:
    """H8: Verifiziert dass /tmp nicht mehr per Request waechst."""

    def test_download_returns_markdown_inline_when_no_md_file(self, app_with_auth):
        """Wenn keine persistierte MD existiert, wird Content direkt als Response geschickt."""
        client = app_with_auth.test_client()

        fake_report = MagicMock()
        fake_report.markdown_content = "# Test Report\n\nInhalt aus dem Speicher."

        with patch(
            "app.api.report.ReportManager.get_report",
            return_value=fake_report,
        ), patch(
            "app.api.report.ReportManager._get_report_markdown_path",
            return_value="/nonexistent/path/that/should/not/exist.md",
        ), patch(
            "app.utils.authz._resource_exists",
            return_value=True,
        ):
            resp = client.get(
                "/api/report/report_deadbeef12345678/download",
                headers={"X-API-Key": _TEST_KEY},
            )

        assert resp.status_code == 200
        # Content-Type sollte markdown sein.
        assert "text/markdown" in resp.headers.get("Content-Type", "").lower()
        # Body enthaelt den Markdown-Content.
        assert b"# Test Report" in resp.data
        assert b"Inhalt aus dem Speicher" in resp.data
        # Content-Disposition mit korrektem Filename.
        cd = resp.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "report_deadbeef12345678.md" in cd

    def test_download_does_not_grow_tmp_dir(self, app_with_auth):
        """/tmp darf bei einem Download mit In-Memory-Content nicht wachsen."""
        client = app_with_auth.test_client()

        fake_report = MagicMock()
        fake_report.markdown_content = "# Leak-Test\n\n" + ("x" * 1000)

        with patch(
            "app.api.report.ReportManager.get_report",
            return_value=fake_report,
        ), patch(
            "app.api.report.ReportManager._get_report_markdown_path",
            return_value="/nonexistent/should/never/exist.md",
        ), patch(
            "app.utils.authz._resource_exists",
            return_value=True,
        ):
            tmp_count_before = _count_tmp_files()

            for _ in range(3):
                resp = client.get(
                    "/api/report/report_cafebabe87654321/download",
                    headers={"X-API-Key": _TEST_KEY},
                )
                assert resp.status_code == 200

            tmp_count_after = _count_tmp_files()

        # Toleranz: andere Tests/Hintergrund-Threads koennen +/-1 verursachen.
        # Vor dem Fix waere der Counter um 3 gewachsen (eine Tempdatei pro
        # Request). Wir akzeptieren maximal +1 als Toleranzbereich.
        assert tmp_count_after - tmp_count_before <= 1, (
            f"Tmp-Verzeichnis ist um {tmp_count_after - tmp_count_before} "
            "Dateien gewachsen — Tempfile-Leak nicht behoben?"
        )

    def test_no_named_temporary_file_with_delete_false_in_codebase(self):
        """Statische Pruefung: Kein NamedTemporaryFile(delete=False) im Backend mehr."""
        backend_app = Path(__file__).resolve().parent.parent / "app"
        offenders = []
        for py_file in backend_app.rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            # Ignore Kommentare und Strings, die das Pattern nur erwaehnen.
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "NamedTemporaryFile" in stripped and "delete=False" in stripped:
                    offenders.append(f"{py_file}:{lineno}: {stripped}")
        assert not offenders, (
            "Mindestens ein NamedTemporaryFile(delete=False)-Aufruf gefunden:\n"
            + "\n".join(offenders)
        )

    def test_download_with_existing_md_file_uses_send_file(self, app_with_auth, tmp_path):
        """Wenn die MD-Datei existiert, wird send_file mit Markdown-Mimetype verwendet."""
        client = app_with_auth.test_client()

        md_file = tmp_path / "report_feedface11223344.md"
        md_file.write_text("# Persistierter Report\n\nDisk-Content", encoding="utf-8")

        fake_report = MagicMock()
        fake_report.markdown_content = "ignored — disk file is preferred"

        with patch(
            "app.api.report.ReportManager.get_report",
            return_value=fake_report,
        ), patch(
            "app.api.report.ReportManager._get_report_markdown_path",
            return_value=str(md_file),
        ), patch(
            "app.utils.authz._resource_exists",
            return_value=True,
        ):
            resp = client.get(
                "/api/report/report_feedface11223344/download",
                headers={"X-API-Key": _TEST_KEY},
            )

        assert resp.status_code == 200
        assert b"Persistierter Report" in resp.data
        assert "text/markdown" in resp.headers.get("Content-Type", "").lower()
