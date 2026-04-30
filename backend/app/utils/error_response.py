"""
Hilfsfunktion für einheitliche, sichere Error-Responses.

Ziel:
- Im Produktiv-Modus (DEBUG=False) wird KEIN Stacktrace und KEINE
  internen Exception-Details an den Client geliefert. Stattdessen erhält
  der Aufrufer eine generische Fehlermeldung sowie eine eindeutige
  Request-ID, die in den Server-Logs nachverfolgt werden kann.
- Im DEBUG-Modus werden Klasse, Nachricht und Traceback zurückgegeben,
  um lokale Fehlersuche zu erleichtern.

Wird konsumiert von:
- ``backend/app/__init__.py`` als ``@app.errorhandler(Exception)``
- direkt aus Routen (graph.py, simulation.py, report.py)
"""

from __future__ import annotations

import traceback
import uuid
from typing import Any

from flask import current_app, g, has_request_context, jsonify, request

from .logger import get_logger

logger = get_logger("mirofish.error")


def _current_request_id() -> str:
    """Liefert eine pro-Request stabile ID (oder eine frische UUID)."""
    if has_request_context():
        rid = getattr(g, "request_id", None)
        if rid:
            return rid
        new_rid = uuid.uuid4().hex
        g.request_id = new_rid
        return new_rid
    return uuid.uuid4().hex


def _is_debug() -> bool:
    """True, wenn die App im Debug-Modus läuft."""
    try:
        return bool(current_app.config.get("DEBUG", False))
    except RuntimeError:
        # Außerhalb eines App-Kontexts: konservativ kein Debug-Detail leaken.
        return False


def format_error_response(exc: BaseException, status: int = 500) -> tuple[Any, int]:
    """Baut eine sichere JSON-Error-Response.

    Args:
        exc: Die aufgetretene Exception.
        status: HTTP-Statuscode (default 500).

    Returns:
        Tuple aus Flask-Response und Statuscode, direkt rückgabefähig.
    """
    request_id = _current_request_id()
    path = request.path if has_request_context() else "<no-request>"

    # Server-seitig immer voll loggen — niemals an den Client.
    logger.exception(
        "Unhandled exception in %s [request_id=%s]",
        path,
        request_id,
    )

    if _is_debug():
        payload = {
            "success": False,
            "request_id": request_id,
            "error": str(exc),
            "error_type": exc.__class__.__name__,
            "traceback": traceback.format_exc(),
        }
    else:
        payload = {
            "success": False,
            "request_id": request_id,
            "error": "Internal server error",
        }

    return jsonify(payload), status
