"""
Tests fuer die Medium-Severity-Audit-Findings im Stream BACKEND-ISOLATED.

Deckt vier disjunkte Fixes ab:

* M1 — Filename-Sanitisierung via ``werkzeug.utils.secure_filename``.
* M7 — Locale auf ``contextvars.ContextVar`` umgestellt (statt thread-locals).
* M8 — Server-side Markdown/HTML-Sanitisierung im Report-Output.
* M10 — Explizite ENV-Allow-List fuer Simulation-Subprozesse.

Die Tests sind so geschrieben, dass sie ohne lebende Flask-App funktionieren
(reine Unit-Tests gegen die jeweiligen Module), damit sie unabhaengig von der
Cross-Cut-Stream-Arbeit (M5/M6) gruen bleiben.
"""

from __future__ import annotations

import asyncio
import os
import threading
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# M1 — secure_filename auf original_filename
# ---------------------------------------------------------------------------


class TestM1FilenameSanitisierung:
    """Original-Filename wird vor Speicherung/Logging entschaerft."""

    def test_path_traversal_components_removed(self, tmp_path):
        """Pfad-Komponenten (.. /) duerfen nicht durchschlagen."""
        from app.models.project import _safe_original_filename

        result = _safe_original_filename("../../etc/passwd")
        assert ".." not in result
        assert "/" not in result
        assert result  # nicht leer

    def test_crlf_and_null_stripped(self):
        """CR/LF und NUL-Bytes werden entfernt (Log-Injection-Schutz)."""
        from app.models.project import _safe_original_filename

        dangerous = "report.pdf\nfoo.exe\x00.sh"
        result = _safe_original_filename(dangerous)
        assert "\n" not in result
        assert "\x00" not in result
        # secure_filename sollte CR/LF zu Underscores oder Punkten reduzieren

    def test_empty_or_only_special_chars_falls_back(self):
        """Wenn Werkzeug nur Sonderzeichen sieht und einen Leerstring zurueckgibt,
        muss ein deterministisches Fallback greifen — niemals Leerstring."""
        from app.models.project import _safe_original_filename

        assert _safe_original_filename("../") == "uploaded_file"
        assert _safe_original_filename("") == "uploaded_file"
        assert _safe_original_filename("///") == "uploaded_file"

    def test_non_string_input_falls_back(self):
        """Type-Hardening — ``None`` oder bytes muessen sicher landen."""
        from app.models.project import _safe_original_filename

        assert _safe_original_filename(None) == "uploaded_file"
        assert _safe_original_filename(b"foo.pdf") == "uploaded_file"

    def test_save_file_to_project_uses_sanitized_name(self, tmp_path, monkeypatch):
        """End-to-end: ``ProjectManager.save_file_to_project`` schreibt einen
        sanitisierten Original-Namen in das returned Dict."""
        from app.models.project import ProjectManager

        # Ueberschreibe Projekt-Wurzel auf tmp_path, damit kein echter Storage
        # angefasst wird.
        monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path / "projects"))
        os.makedirs(str(tmp_path / "projects"), exist_ok=True)

        project = ProjectManager.create_project(name="m1-smoke-test")

        # FileStorage-Stub: nur ``.save(path)`` wird gebraucht.
        file_storage = MagicMock()

        def fake_save(path):
            # L6-Magic-Check verlangt %PDF-Header — vorher reichte beliebiger
            # Inhalt fuer den M1-Test; jetzt brauchen wir gueltige Magic-Bytes.
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4\nfake-pdf-payload")

        file_storage.save.side_effect = fake_save

        info = ProjectManager.save_file_to_project(
            project.project_id,
            file_storage,
            "../../etc/sneaky.pdf",
        )

        # Original-Filename ist sanitisiert: kein "../"
        assert ".." not in info["original_filename"]
        assert "/" not in info["original_filename"]
        assert info["original_filename"].endswith(".pdf")


# ---------------------------------------------------------------------------
# M7 — Locale via contextvars (Async-/Thread-sicher)
# ---------------------------------------------------------------------------


class TestM7LocaleContextvars:
    """``set_locale`` darf nicht zwischen Threads/Async-Tasks lecken."""

    def test_setlocale_isolated_per_thread(self):
        """Worker-Thread sieht nicht die Locale des aufrufenden Threads."""
        from app.utils.locale import get_locale, set_locale

        set_locale("en")
        assert get_locale() == "en"

        seen = {}

        def worker():
            # Default ist ``zh`` — Worker-Thread darf nicht ``en`` sehen.
            seen["thread"] = get_locale()
            set_locale("zh")
            seen["thread_after"] = get_locale()

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert seen["thread"] == "zh"
        assert seen["thread_after"] == "zh"
        # Caller sieht weiter ``en``
        assert get_locale() == "en"

    def test_setlocale_isolated_per_async_task(self):
        """Async-Tasks haben unabhaengige Locale-Kontexte (ContextVar-Verhalten)."""
        from app.utils.locale import get_locale, set_locale

        async def task_a():
            set_locale("en")
            await asyncio.sleep(0)
            return get_locale()

        async def task_b():
            set_locale("zh")
            await asyncio.sleep(0)
            return get_locale()

        async def runner():
            return await asyncio.gather(task_a(), task_b())

        result = asyncio.run(runner())
        assert "en" in result
        assert "zh" in result


# ---------------------------------------------------------------------------
# M8 — Server-side Markdown/HTML Sanitisierung
# ---------------------------------------------------------------------------


class TestM8MarkdownSanitisierung:
    """``sanitize_markdown`` strippt gefaehrliche HTML-Tags aus Markdown-Content."""

    def test_script_tag_removed(self):
        from app.utils.markdown_sanitizer import sanitize_markdown

        evil = "# Title\n\nNormal text\n<script>alert(1)</script>"
        clean = sanitize_markdown(evil)
        assert "<script>" not in clean
        assert "alert(1)" not in clean or "</script>" not in clean
        # Markdown-Headings bleiben intakt
        assert "# Title" in clean

    def test_iframe_and_onerror_removed(self):
        from app.utils.markdown_sanitizer import sanitize_markdown

        evil = '<iframe src="evil.com"></iframe><img src=x onerror=alert(1)>'
        clean = sanitize_markdown(evil)
        assert "<iframe" not in clean
        assert "onerror" not in clean

    def test_safe_inline_html_preserved(self):
        """Markdown erlaubt safe-HTML-Tags (b, i, em, code) — diese muessen bleiben."""
        from app.utils.markdown_sanitizer import sanitize_markdown

        safe = "Some **bold** and <em>italic</em> text with <code>inline</code>."
        clean = sanitize_markdown(safe)
        assert "**bold**" in clean
        assert "<em>" in clean
        assert "<code>" in clean

    def test_none_or_empty_input_returns_string(self):
        from app.utils.markdown_sanitizer import sanitize_markdown

        assert sanitize_markdown("") == ""
        assert sanitize_markdown(None) == ""


# ---------------------------------------------------------------------------
# M10 — ENV-Allow-List fuer Subprozesse
# ---------------------------------------------------------------------------


class TestM10EnvAllowlist:
    """SimulationRunner darf nicht das gesamte ``os.environ`` an Worker
    weiterreichen — nur eine kuratierte Allow-List."""

    def test_build_subprocess_env_strips_unrelated(self, monkeypatch):
        from app.services.simulation_runner import build_subprocess_env

        monkeypatch.setenv("LLM_API_KEY", "sk-test-1234")
        monkeypatch.setenv("LLM_BASE_URL", "https://example/v1")
        monkeypatch.setenv("LLM_MODEL_NAME", "gpt-test")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/test")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leaky-aws")
        monkeypatch.setenv("GITHUB_TOKEN", "leaky-github")
        monkeypatch.setenv("DATABASE_URL", "postgres://leak")

        env = build_subprocess_env()

        assert env["LLM_API_KEY"] == "sk-test-1234"
        assert env["LLM_BASE_URL"] == "https://example/v1"
        assert env["LLM_MODEL_NAME"] == "gpt-test"
        assert env["PATH"] == "/usr/bin:/bin"
        assert env["HOME"] == "/home/test"

        # Diese muessen GESTRIPT sein
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "GITHUB_TOKEN" not in env
        assert "DATABASE_URL" not in env

    def test_build_subprocess_env_keeps_locale_vars(self, monkeypatch):
        from app.services.simulation_runner import build_subprocess_env

        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
        monkeypatch.setenv("PYTHONIOENCODING", "utf-8")

        env = build_subprocess_env()
        assert env.get("LANG") == "en_US.UTF-8"
        assert env.get("LC_ALL") == "en_US.UTF-8"
        # PYTHONIOENCODING wird vom Runner explizit gesetzt — Allow-List
        # erlaubt ihn als POSIX-Default.
        assert env.get("PYTHONIOENCODING") == "utf-8"

    def test_build_subprocess_env_keeps_oasis_and_lightrag(self, monkeypatch):
        from app.services.simulation_runner import build_subprocess_env

        monkeypatch.setenv("OASIS_DEFAULT_MAX_ROUNDS", "5")
        monkeypatch.setenv("LIGHTRAG_WORKING_DIR", "/var/data/rag")
        monkeypatch.setenv("MIROFISH_API_KEY", "mf-key-xyz")

        env = build_subprocess_env()
        assert env["OASIS_DEFAULT_MAX_ROUNDS"] == "5"
        assert env["LIGHTRAG_WORKING_DIR"] == "/var/data/rag"
        assert env["MIROFISH_API_KEY"] == "mf-key-xyz"
