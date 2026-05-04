"""
Locale-Resolver fuer Backend-Threads, Async-Tasks und Flask-Requests.

Fix M7 (Audit Medium): Die alte Implementierung benutzte
``threading.local()``. Das war OK fuer Worker-Threads (die ihre Locale
explizit via ``set_locale`` setzen), bricht aber unter
``asyncio``: Mehrere coroutines im selben Thread teilen sich denselben
``threading.local`` und ueberschreiben gegenseitig ihre Locale. Mit
LightRAG-Migration laufen RAG-Calls async im Worker-Pool und koennen
Locale durch Race Conditions falsch zuruecksetzen.

Loesung: ``contextvars.ContextVar``. Async-Tasks erben den Context
beim Spawn, koennen ihn lokal mutieren, ohne Geschwister-Tasks zu
beeinflussen, und Threads erhalten beim Start einen frischen Default.
"""

import json
import os
from contextvars import ContextVar

from flask import has_request_context, request


_locales_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'locales')

# Load language registry
with open(os.path.join(_locales_dir, 'languages.json'), 'r', encoding='utf-8') as f:
    _languages = json.load(f)

# Load translation files
_translations = {}
for filename in os.listdir(_locales_dir):
    if filename.endswith('.json') and filename != 'languages.json':
        locale_name = filename[:-5]
        with open(os.path.join(_locales_dir, filename), 'r', encoding='utf-8') as f:
            _translations[locale_name] = json.load(f)


# Default ist absichtlich ``None``, sodass ``get_locale`` zwischen
# "explizit ``zh`` gesetzt" und "noch nichts gesetzt" unterscheiden
# kann. Faellt auf ``zh`` zurueck.
_locale_var: ContextVar[str | None] = ContextVar('mirofish_locale', default=None)


def set_locale(locale: str) -> None:
    """Setze Locale fuer den aktuellen Async-Context bzw. Thread.

    Beim Spawn eines neuen Threads wird die ContextVar auf den Default
    (``None``) zurueckgesetzt — kein Leak zwischen Threads.
    """
    _locale_var.set(locale)


def get_locale() -> str:
    """Liefere die aktive Locale.

    Reihenfolge: 1) Flask-Request-Header (wenn im Request-Context),
    2) ContextVar (gesetzt durch ``set_locale``), 3) Fallback ``zh``.
    """
    if has_request_context():
        raw = request.headers.get('Accept-Language', 'zh')
        return raw if raw in _translations else 'zh'
    value = _locale_var.get()
    return value if value is not None else 'zh'


def t(key: str, **kwargs) -> str:
    locale = get_locale()
    messages = _translations.get(locale, _translations.get('zh', {}))

    value = messages
    for part in key.split('.'):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    if value is None:
        value = _translations.get('zh', {})
        for part in key.split('.'):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

    if value is None:
        return key

    if kwargs:
        for k, v in kwargs.items():
            value = value.replace(f'{{{k}}}', str(v))

    return value


def get_language_instruction() -> str:
    locale = get_locale()
    lang_config = _languages.get(locale, _languages.get('zh', {}))
    return lang_config.get('llmInstruction', '请使用中文回答。')
