"""
Rate-Limiter (Audit-Finding H6).

Single-Source-of-Truth fuer den global geteilten ``flask-limiter``-Limiter.
Routen importieren ``limiter`` aus diesem Modul und benutzen
``@limiter.limit(...)`` als Dekorator. Die Konfiguration kommt aus
``Config.RATE_LIMIT_*``; ``RATE_LIMIT_ENABLED=False`` schaltet alle Limits
fuer Tests/Dev ab (limiter.enabled = False).

Schluessel-Funktion (``key_func``): X-API-Key-Header. MiroFish ist ein
Single-User-System mit Auth via X-API-Key (C1). Ein IP-basiertes Limit
ist hier sinnlos, weil typischerweise Frontend (Vue) <-> Backend (Flask)
auf localhost laufen. Faellt der Header zurueck auf 'no-api-key', schlaegt
Auth-Middleware bereits an und es gibt nie einen tatsaechlichen Bypass.
"""

from __future__ import annotations

from flask import request
from flask_limiter import Limiter

from ..config import Config


def _api_key_identifier() -> str:
    """Liefert den X-API-Key als Limiter-Schluessel."""
    return request.headers.get('X-API-Key', 'no-api-key')


# Globaler Limiter — wird in app/__init__.py per ``limiter.init_app(app)``
# an die Flask-App gebunden. Vor init_app sind alle ``@limiter.limit(...)``-
# Dekorationen vorgemerkt, aber inaktiv.
limiter = Limiter(
    key_func=_api_key_identifier,
    default_limits=[Config.RATE_LIMIT_DEFAULT],
    # In-memory ist ausreichend fuer Single-Instance/Single-User-Setup.
    # Bei Multi-Worker-Deployment muesste hier 'redis://...' rein.
    storage_uri='memory://',
    headers_enabled=True,  # X-RateLimit-Limit / -Remaining / -Reset im Response
)
