"""
Tests fuer Audit-Finding C6 — Path-Traversal-Schutz.

Deckt zwei Ebenen ab:

1. ``app.utils.safe_id`` — die Validator-Util selbst (safe_id, safe_path_under,
   safe_filename). Whitelist-Format, Traversal-Erkennung, Symlink-Pruefung.

2. Konsumenten-Smoke — ProjectManager._get_project_dir und ReportManager.
   _get_report_folder muessen ungueltige IDs ablehnen.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app.utils.safe_id import safe_filename, safe_id, safe_path_under


# ---------------------------------------------------------------------------
# safe_id
# ---------------------------------------------------------------------------


class TestSafeId:
    def test_valid_proj_id_passes(self):
        assert safe_id("proj_abc12345") == "proj_abc12345"

    def test_valid_sim_id_passes(self):
        assert safe_id("sim_a1b2c3d4e5f67890") == "sim_a1b2c3d4e5f67890"

    def test_valid_report_id_passes(self):
        assert safe_id("report_deadbeefcafebabe") == "report_deadbeefcafebabe"

    def test_valid_task_id_passes(self):
        assert safe_id("task_0123456789ab") == "task_0123456789ab"

    def test_prefix_match_passes(self):
        assert safe_id("proj_abc12345", prefix="proj") == "proj_abc12345"

    def test_prefix_mismatch_rejected(self):
        with pytest.raises(ValueError):
            safe_id("sim_abc12345", prefix="proj")

    def test_unknown_prefix_argument_rejected(self):
        with pytest.raises(ValueError):
            safe_id("proj_abc12345", prefix="evil")

    @pytest.mark.parametrize(
        "bad",
        [
            "../etc/passwd",
            "proj_../etc",
            "proj_..",
            "proj_/abc12345",
            "proj_abc",                 # zu kurz
            "proj_" + "a" * 33,         # zu lang
            "PROJ_abc12345",            # falsche Praefix-Schreibweise
            "proj_ABC12345",            # nicht-hex
            "proj_abc12345.json",       # Extension
            "proj_abc 12345",           # whitespace
            "evil_abc12345",            # unbekannter Praefix
            "",
            "abc12345",                 # ohne Praefix
        ],
    )
    def test_invalid_ids_rejected(self, bad):
        with pytest.raises(ValueError):
            safe_id(bad)

    def test_non_string_rejected(self):
        with pytest.raises(ValueError):
            safe_id(123)  # type: ignore[arg-type]

    def test_none_rejected(self):
        with pytest.raises(ValueError):
            safe_id(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# safe_path_under
# ---------------------------------------------------------------------------


class TestSafePathUnder:
    def test_simple_join_under_base(self, tmp_path: Path):
        base = str(tmp_path)
        result = safe_path_under(base, "proj_abc12345")
        assert result.startswith(os.path.realpath(base))

    def test_traversal_dotdot_rejected(self, tmp_path: Path):
        base = str(tmp_path)
        with pytest.raises(ValueError):
            safe_path_under(base, "..")

    def test_traversal_in_subpart_rejected(self, tmp_path: Path):
        base = str(tmp_path)
        with pytest.raises(ValueError):
            safe_path_under(base, "../..", "etc")

    def test_absolute_part_rejected(self, tmp_path: Path):
        base = str(tmp_path)
        # Absolute Komponenten (wie '/etc/passwd') ueberschreiben den
        # base-Praefix per Posix-Semantik — safe_path_under muss das fangen.
        with pytest.raises(ValueError):
            safe_path_under(base, "/etc/passwd")

    def test_symlink_escape_rejected(self, tmp_path: Path):
        # Symlink, der aus dem Base-Verzeichnis raus zeigt.
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        try:
            base = tmp_path / "base"
            base.mkdir()
            link = base / "evil"
            try:
                os.symlink(str(outside), str(link))
            except (OSError, NotImplementedError):
                pytest.skip("Symlinks nicht unterstuetzt auf dieser Plattform")
            with pytest.raises(ValueError):
                safe_path_under(str(base), "evil")
        finally:
            if outside.exists():
                outside.rmdir()

    def test_no_parts_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            safe_path_under(str(tmp_path))

    def test_nested_valid_path(self, tmp_path: Path):
        result = safe_path_under(str(tmp_path), "proj_abc12345", "files", "data.txt")
        real_base = os.path.realpath(str(tmp_path))
        assert result.startswith(real_base)


# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------


class TestSafeFilename:
    def test_simple_filename(self):
        assert safe_filename("simulation_config.json") == "simulation_config.json"

    def test_extension_whitelist_passes(self):
        assert safe_filename("config.json", allowed_ext=["json"]) == "config.json"

    def test_extension_whitelist_rejects(self):
        with pytest.raises(ValueError):
            safe_filename("config.exe", allowed_ext=["json"])

    @pytest.mark.parametrize(
        "bad",
        [
            "../config.json",
            "/etc/passwd",
            "evil\x00",
            "",
            "config; rm -rf /.json",
            "config\nname.json",
        ],
    )
    def test_bad_filenames_rejected(self, bad):
        with pytest.raises(ValueError):
            safe_filename(bad)


# ---------------------------------------------------------------------------
# ProjectManager smoke — _get_project_dir + delete_project
# ---------------------------------------------------------------------------


class TestProjectManagerSafeId:
    def test_invalid_id_raises_in_get_project_dir(self):
        from app.models.project import ProjectManager

        with pytest.raises(ValueError):
            ProjectManager._get_project_dir("../etc")

    def test_delete_project_invalid_id_returns_false(self):
        from app.models.project import ProjectManager

        assert ProjectManager.delete_project("../evil") is False

    def test_delete_project_unknown_proper_id_returns_false(self):
        from app.models.project import ProjectManager

        # Format-valide, aber Verzeichnis existiert nicht — Funktion gibt False.
        assert ProjectManager.delete_project("proj_deadbeef0000") is False


# ---------------------------------------------------------------------------
# ReportManager smoke — _get_report_folder
# ---------------------------------------------------------------------------


class TestReportManagerSafeId:
    def test_invalid_id_raises(self):
        from app.services.report_agent import ReportManager

        with pytest.raises(ValueError):
            ReportManager._get_report_folder("../evil")

    def test_valid_id_returns_path_under_root(self, tmp_path: Path):
        from app.services.report_agent import ReportManager

        result = ReportManager._get_report_folder("report_deadbeef1234")
        # Egal welcher Reports-Root konfiguriert ist — der Pfad muss
        # darunter liegen.
        real_root = os.path.realpath(ReportManager.REPORTS_DIR)
        assert os.path.realpath(result).startswith(real_root)
