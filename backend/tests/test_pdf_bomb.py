"""
Tests fuer Audit-Finding H7 — PDF-Bombe / kein PyMuPDF-Limit.

Vorher: ``backend/app/utils/file_parser.py:97-111`` rief ``fitz.open(file_path)``
ohne Page- oder Decoded-Size-Limit. Eine 50 MB komprimierte PDF mit Millionen
Seiten oder mehreren GB dekomprimiertem Text loest OOM aus.

Fix: ``Config.PDF_MAX_PAGES`` (default 500) und ``Config.PDF_MAX_EXTRACTED_BYTES``
(default 5_000_000) werden vor und waehrend der Extraktion geprueft.
``ValueError`` wird sofort geworfen, wenn ein Limit ueberschritten wird.

Diese Tests verifizieren:

1. PDF mit Page-Anzahl ueber Limit -> ValueError mit klarer Meldung.
2. PDF mit zu viel extrahiertem Text -> ValueError nach laufendem Counter.
3. Normale PDF (1 Seite, kurzer Text) -> Extraktion liefert Text zurueck.
4. Default-Limits aus Config sind gesetzt.
5. Flask-Upload-Limit (MAX_CONTENT_LENGTH = 50 MB) deckt zumindest die
   komprimierte Eingabe ab — als zweite Verteidigungslinie verifizieren.
"""

from __future__ import annotations

import io

import fitz  # PyMuPDF
import pytest

from app.config import Config
from app.utils.file_parser import FileParser


def _make_pdf_with_pages(num_pages: int, page_text: str = "") -> bytes:
    """Erzeugt eine PDF mit ``num_pages`` Seiten und optionalem Text pro Seite."""
    doc = fitz.open()
    try:
        for _ in range(num_pages):
            page = doc.new_page()
            if page_text:
                page.insert_text((50, 72), page_text, fontsize=11)
        return doc.tobytes()
    finally:
        doc.close()


class TestPdfBomb:
    """H7: Verifiziert Page-Limit und Decoded-Size-Limit fuer PDF-Extraktion."""

    def test_normal_pdf_extracts_successfully(self, tmp_path):
        """Eine normale 1-Seiten-PDF mit kurzem Text wird erfolgreich extrahiert."""
        pdf_path = tmp_path / "normal.pdf"
        pdf_path.write_bytes(_make_pdf_with_pages(1, "Hallo Welt!"))

        text = FileParser._extract_from_pdf(str(pdf_path))
        assert "Hallo" in text or "Welt" in text  # PyMuPDF kann je nach Font abweichen

    def test_pdf_over_page_limit_raises(self, tmp_path, monkeypatch):
        """PDF mit mehr Seiten als ``PDF_MAX_PAGES`` wirft ValueError."""
        # Niedriges Limit fuer den Test, damit wir keine Mega-PDF bauen muessen.
        monkeypatch.setattr(Config, "PDF_MAX_PAGES", 5, raising=False)
        monkeypatch.setattr(
            Config, "PDF_MAX_EXTRACTED_BYTES", 5_000_000, raising=False
        )

        pdf_path = tmp_path / "too_many_pages.pdf"
        pdf_path.write_bytes(_make_pdf_with_pages(20, "x"))

        with pytest.raises(ValueError) as exc_info:
            FileParser._extract_from_pdf(str(pdf_path))

        msg = str(exc_info.value)
        assert "Seiten" in msg or "pages" in msg.lower()
        assert "20" in msg  # tatsaechliche Anzahl
        assert "5" in msg   # Limit

    def test_pdf_over_text_size_limit_raises(self, tmp_path, monkeypatch):
        """PDF mit zu viel extrahiertem Text wirft ValueError waehrend der Extraktion."""
        # Sehr niedriges Byte-Limit, damit eine 5-Seiten-PDF mit langen Strings es reisst.
        monkeypatch.setattr(Config, "PDF_MAX_PAGES", 500, raising=False)
        monkeypatch.setattr(Config, "PDF_MAX_EXTRACTED_BYTES", 100, raising=False)

        # 10 Seiten a ca. 50 Zeichen -> ueber 100 Bytes nach 2-3 Seiten.
        long_text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 3
        pdf_path = tmp_path / "text_bomb.pdf"
        pdf_path.write_bytes(_make_pdf_with_pages(10, long_text))

        with pytest.raises(ValueError) as exc_info:
            FileParser._extract_from_pdf(str(pdf_path))

        msg = str(exc_info.value)
        assert "Bytes" in msg or "Bomb" in msg or "PDF" in msg

    def test_config_defaults_are_set(self):
        """Default-Limits muessen vorhanden und sinnvoll sein."""
        assert Config.PDF_MAX_PAGES > 0
        assert Config.PDF_MAX_PAGES <= 10000  # Sanity: nicht "kein Limit"
        assert Config.PDF_MAX_EXTRACTED_BYTES > 0
        # Mindestens 1 MB, hoechstens 100 MB Default — Tuning-Spielraum.
        assert 1_000_000 <= Config.PDF_MAX_EXTRACTED_BYTES <= 100_000_000

    def test_flask_max_content_length_caps_compressed_input(self):
        """Verteidigungslinie 1: Flask MAX_CONTENT_LENGTH begrenzt komprimierten Upload."""
        # 50 MB ist der dokumentierte Wert. Wenn er kleiner wird, ist das nicht
        # automatisch ein Bug, aber wir wollen wissen wenn er sich aendert.
        assert Config.MAX_CONTENT_LENGTH >= 1_000_000  # mindestens 1 MB
        assert Config.MAX_CONTENT_LENGTH <= 200 * 1024 * 1024  # max 200 MB

    def test_pdf_at_exactly_page_limit_passes(self, tmp_path, monkeypatch):
        """PDF mit ``page_count == max_pages`` darf passieren (off-by-one-Schutz)."""
        monkeypatch.setattr(Config, "PDF_MAX_PAGES", 3, raising=False)
        monkeypatch.setattr(
            Config, "PDF_MAX_EXTRACTED_BYTES", 5_000_000, raising=False
        )

        pdf_path = tmp_path / "exact_limit.pdf"
        pdf_path.write_bytes(_make_pdf_with_pages(3, "ok"))

        # Sollte NICHT werfen.
        text = FileParser._extract_from_pdf(str(pdf_path))
        # Inhalt unwichtig, Hauptsache kein Raise.
        assert isinstance(text, str)
