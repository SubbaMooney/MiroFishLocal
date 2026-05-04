"""
Server-side Markdown/HTML-Sanitizer.

Fix M8 (Audit Medium, Defense-in-Depth zu H1):
LLM-generierter Markdown-Content kann eingebettetes HTML enthalten
(``<script>``, ``<iframe>``, ``onerror``-Handler usw.). Auch wenn das
Frontend mittels ``marked + DOMPurify`` clientseitig saubert (H1), ist
serverseitige Sanitisierung Pflicht, damit:

* der gespeicherte Markdown selbst clean ist und keine spaeteren
  Konsumenten (Export, E-Mail-Render, Re-Ingest) gefaehrdet werden,
* gegen einen kompromittierten/ausgeschalteten Frontend-Filter
  Defense-in-Depth besteht.

Whitelist-Strategie:
``bleach.clean`` mit einer konservativen Liste von Inline-Tags, die
fuer die LLM-Reports inhaltlich sinnvoll sind. Block-Strukturen
(Headings, Listen, Codeblocks) bleiben als reines Markdown erhalten —
``marked`` rendert sie spaeter clientseitig. Alle nicht-whitelisteten
HTML-Tags werden entfernt (``strip=True``).
"""

from __future__ import annotations

import bleach


# Markdown erlaubt eine kleine Auswahl an Inline-HTML — diese Tags
# bleiben erhalten. Block-Tags (``div``, ``table``, ``script``, …)
# sind absichtlich nicht enthalten.
_ALLOWED_TAGS: frozenset[str] = frozenset({
    'a', 'abbr', 'b', 'br', 'code', 'em', 'i', 'kbd', 'mark',
    's', 'small', 'span', 'strong', 'sub', 'sup', 'u',
})

# Attribute-Whitelist pro Tag. Bewusst minimal — keine ``style``,
# kein ``on*``-Handler, kein ``javascript:``-href.
_ALLOWED_ATTRS: dict[str, list[str]] = {
    'a': ['href', 'title', 'rel'],
    'abbr': ['title'],
    'span': ['title'],
}

# Erlaubte URL-Schemes fuer ``href`` — ``javascript:`` ist absichtlich
# ausgeschlossen.
_ALLOWED_PROTOCOLS: list[str] = ['http', 'https', 'mailto']


def sanitize_markdown(content: str | None) -> str:
    """Entferne gefaehrliche HTML-Tags aus Markdown-Content.

    Markdown-Syntax (``# heading``, ``**bold**``, ``- list``, code-fences)
    wird nicht beruehrt — nur eingebettete HTML-Fragmente werden
    gegen die Whitelist abgeglichen. Nicht-erlaubte Tags werden
    komplett entfernt (Inhalt bleibt als Text).

    Args:
        content: Markdown-Text mit potentiell eingebetteten HTML-Tags.
                 ``None`` und Leerstring sind tolerant erlaubt.

    Returns:
        Sanitisierter Markdown-Text.
    """
    if not content:
        return ''
    if not isinstance(content, str):
        # Defensiv — niemals andere Typen passieren lassen.
        content = str(content)

    return bleach.clean(
        content,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,  # Tags ENTFERNEN, nicht escapen
        strip_comments=True,
    )
