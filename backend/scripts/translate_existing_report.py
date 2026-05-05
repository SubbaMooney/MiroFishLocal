"""
One-shot: Uebersetze chinesische Inhalte in einem bereits generierten Report
ins Englische und schreibe die Files in-place zurueck. Idempotent: bereits
englische Sections werden uebersprungen.

Usage:
    cd backend && uv run python scripts/translate_existing_report.py <report_id>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Backend-Pfade einbinden, damit die App-Module geladen werden koennen.
_repo_root = Path(__file__).resolve().parents[2]
_backend = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_backend))

from app.config import Config  # noqa: E402
from app.utils.llm_client import LLMClient  # noqa: E402

# CJK Unicode-Range — wenn ein Section-Body mehr als 5% CJK enthaelt,
# behandeln wir ihn als chinesisch und uebersetzen.
_CJK_RE = re.compile(r"[一-鿿]")
_CJK_RATIO_THRESHOLD = 0.05

TRANSLATE_SYSTEM = (
    "[LANGUAGE REQUIREMENT] You MUST respond exclusively in English. "
    "Translate the user's text from Chinese to professional, fluent English. "
    "Preserve all Markdown formatting (headers, bold, blockquotes, lists, "
    "tables) exactly. Preserve quotation marks. Preserve named entities "
    "verbatim. Do NOT add commentary, notes, or 'Translation:' prefixes — "
    "output only the translated text."
)


def looks_chinese(text: str) -> bool:
    if not text:
        return False
    cjk = len(_CJK_RE.findall(text))
    return cjk / max(1, len(text)) >= _CJK_RATIO_THRESHOLD


def translate_to_english(client: LLMClient, text: str) -> str:
    """Sicht: Tokens werden via TokenTracker mitgezaehlt (purpose='translate')."""
    return client.chat(
        messages=[
            {"role": "system", "content": TRANSLATE_SYSTEM},
            {"role": "user", "content": text},
        ],
        temperature=0.0,
        max_tokens=4096,
        purpose="translate:report-section",
    )


def process_section_file(client: LLMClient, path: Path) -> bool:
    """Liest, uebersetzt wenn noetig, schreibt. Return True wenn uebersetzt wurde."""
    body = path.read_text(encoding="utf-8")
    if not looks_chinese(body):
        print(f"  - {path.name}: bereits englisch, skip.")
        return False
    print(f"  > {path.name}: chinesisch, uebersetze ({len(body)} chars)...")
    translated = translate_to_english(client, body)
    path.write_text(translated, encoding="utf-8")
    print(f"    geschrieben ({len(translated)} chars).")
    return True


def process_meta(client: LLMClient, path: Path) -> bool:
    """Outline-Section-Contents in meta.json uebersetzen."""
    meta = json.loads(path.read_text(encoding="utf-8"))
    outline = meta.get("outline") or {}
    sections = outline.get("sections") or []
    changed = False
    for sec in sections:
        content = sec.get("content") or ""
        if looks_chinese(content):
            print(f"  > meta.outline.sections[{sec.get('title')}]: uebersetze...")
            sec["content"] = translate_to_english(client, content)
            changed = True
    summary = outline.get("summary") or ""
    if looks_chinese(summary):
        print("  > meta.outline.summary: uebersetze...")
        outline["summary"] = translate_to_english(client, summary)
        changed = True
    if changed:
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print("    meta.json geschrieben.")
    else:
        print("  - meta.json: keine chinesischen Inhalte gefunden, skip.")
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: translate_existing_report.py <report_id>")
        return 1
    report_id = sys.argv[1]
    reports_root = Path(Config.UPLOAD_FOLDER) / "reports" / report_id
    if not reports_root.exists():
        print(f"Report-Ordner nicht gefunden: {reports_root}")
        return 1

    print(f"Translating report {report_id} (root: {reports_root})")
    client = LLMClient()

    section_files = sorted(reports_root.glob("section_*.md"))
    print(f"Gefundene Section-Files: {len(section_files)}")

    translated_any = False
    for sf in section_files:
        translated_any |= process_section_file(client, sf)

    meta_path = reports_root / "meta.json"
    if meta_path.exists():
        translated_any |= process_meta(client, meta_path)

    full_report = reports_root / "full_report.md"
    if section_files and translated_any:
        # full_report neu zusammensetzen aus dem Outline-Titel + Sections.
        meta = (
            json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_path.exists()
            else {}
        )
        outline = meta.get("outline") or {}
        title = outline.get("title", "Report")
        summary = outline.get("summary", "")
        parts = [f"# {title}", "", summary, ""]
        for sf in section_files:
            parts.append(sf.read_text(encoding="utf-8"))
            parts.append("")
        full_report.write_text("\n".join(parts), encoding="utf-8")
        print(f"  > full_report.md neu geschrieben ({full_report.stat().st_size} bytes)")

    print("Fertig." if translated_any else "Keine Aenderung noetig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
