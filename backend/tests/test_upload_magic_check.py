"""
Tests fuer Audit-Finding L6 — Magic-Number-Check vor Upload-Akzeptanz.
"""

from __future__ import annotations

import os
from io import BytesIO

import pytest

from app.models.project import _validate_upload_content


@pytest.fixture
def tmp_file(tmp_path):
    def _make(content: bytes, name: str) -> str:
        p = tmp_path / name
        p.write_bytes(content)
        return str(p)

    return _make


class TestPdfMagic:
    def test_valid_pdf_accepted(self, tmp_file):
        path = tmp_file(b"%PDF-1.4\n...", "doc.pdf")
        # Keine Exception erwartet.
        _validate_upload_content(path, ".pdf")

    def test_missing_magic_rejected(self, tmp_file):
        path = tmp_file(b"this is not a pdf", "fake.pdf")
        with pytest.raises(ValueError, match="PDF-Magic fehlt"):
            _validate_upload_content(path, ".pdf")


class TestTextBlockedBinaries:
    def test_elf_rejected_as_md(self, tmp_file):
        path = tmp_file(b"\x7fELF\x02\x01\x01", "evil.md")
        with pytest.raises(ValueError, match="binaere Marker"):
            _validate_upload_content(path, ".md")

    def test_pe_rejected_as_txt(self, tmp_file):
        path = tmp_file(b"MZ\x90\x00", "evil.txt")
        with pytest.raises(ValueError, match="binaere Marker"):
            _validate_upload_content(path, ".txt")

    def test_zip_rejected_as_txt(self, tmp_file):
        path = tmp_file(b"PK\x03\x04zipdata", "evil.txt")
        with pytest.raises(ValueError, match="binaere Marker"):
            _validate_upload_content(path, ".txt")

    def test_shebang_rejected(self, tmp_file):
        path = tmp_file(b"#!/bin/bash\nrm -rf /", "evil.md")
        with pytest.raises(ValueError, match="binaere Marker"):
            _validate_upload_content(path, ".md")

    def test_normal_text_accepted(self, tmp_file):
        path = tmp_file("Hallo Welt — Test\n".encode("utf-8"), "ok.md")
        _validate_upload_content(path, ".md")


class TestUnknownExtension:
    def test_unknown_ext_rejected(self, tmp_file):
        path = tmp_file(b"anything", "x.exe")
        with pytest.raises(ValueError, match="nicht in Whitelist"):
            _validate_upload_content(path, ".exe")
