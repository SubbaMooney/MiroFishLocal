"""
Audit M6 — JSON-Schema-Validation an System-Boundaries.

Pydantic-v2-basierter Decorator, der den Request-Body gegen ein Schema
validiert. Bei Verletzung 400 mit ``format_error_response``. Das geparste
Modell landet in ``flask.g.validated_body`` und kann von der Route
optional genutzt werden — bestehende ``data.get(...)``-Pfade bleiben
gleichzeitig kompatibel, weil Flask die JSON-Payload bereits gecached
hat.

Designentscheidung: Decorator vor ``@limiter.limit`` und vor
``@require_resource`` zu setzen ist nicht zwingend, aber empfohlen —
ungueltige Bodies sollten 400 werfen, bevor wir Rate-Limit-Quota oder
Resource-Lookups verbrauchen. Praktisch ist die Anordnung:

    @bp.route(..., methods=['POST'])
    @limiter.limit(...)
    @validate_body(MySchema)
    def handler(): ...

(Limiter zuerst, weil er Anti-DoS ist; danach Schema, weil es 400 statt
500 produziert; danach evtl. Auth-Decorator.)
"""

from __future__ import annotations

from functools import wraps
from typing import Type

from flask import g, request
from pydantic import BaseModel, ValidationError

from .error_response import format_error_response


def validate_body(schema_cls: Type[BaseModel]):
    """Dekorator: validiert ``request.get_json()`` gegen ``schema_cls``.

    Bei Erfolg landet die Pydantic-Instanz in ``flask.g.validated_body``.
    Bei Validation-Fehler 400 mit kompakter Fehlerliste.
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            payload = request.get_json(silent=True)
            if payload is None:
                payload = {}
            try:
                model = schema_cls.model_validate(payload)
            except ValidationError as exc:
                # Kompakte Liste: nur Feld-Pfad und Message.
                # Keine internal types/contexts leaken (vgl. C5).
                details = [
                    {
                        "field": ".".join(str(p) for p in err.get("loc", ())),
                        "message": err.get("msg", "invalid"),
                    }
                    for err in exc.errors()
                ]
                err = ValueError(
                    f"Body-Validation fehlgeschlagen: {len(details)} Fehler"
                )
                resp = format_error_response(err, status=400)
                # format_error_response liefert (Response, status). Wir
                # haengen die details direkt an — sie sind generisch
                # genug fuer den Client.
                response, status = resp
                payload_obj = response.get_json() or {}
                payload_obj["validation_errors"] = details
                response.set_data(
                    __import__("json").dumps(payload_obj, ensure_ascii=False)
                )
                return response, status
            g.validated_body = model
            return fn(*args, **kwargs)

        return wrapper

    return decorator
