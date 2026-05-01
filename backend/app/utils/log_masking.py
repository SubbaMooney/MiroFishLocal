"""
Hilfsfunktion zum Maskieren sensibler Felder in Logging-Payloads.

Wir loggen im Debug-Modus die JSON-Bodies eingehender Requests. Damit
gelangen sonst Klartext-Tokens, Passwörter und API-Keys ungefiltert in
unsere Log-Dateien — selbst wenn die Felder absolut nichts mit dem
eigentlichen Request-Inhalt zu tun haben.

Diese Hilfsfunktion ersetzt die Werte aller Felder, deren Name auf eine
bekannte Sensibel-Liste passt (case-insensitive Substring-Match), durch
``"***"``. Sie arbeitet rekursiv durch verschachtelte ``dict``- und
``list``-Strukturen.
"""

from __future__ import annotations

from typing import Any

# Substrings, die — case-insensitive — in einem Feldnamen vorkommen müssen,
# damit der Wert maskiert wird. Bewusst breit gefasst, damit Varianten wie
# ``user_password`` oder ``X-API-Key`` ebenfalls erkannt werden.
SENSITIVE_FIELD_HINTS: tuple[str, ...] = (
    "password",
    "token",
    "api_key",
    "apikey",
    "secret_key",
    "secretkey",
    "secret",
    "llm_api_key",
    "zep_api_key",
    "authorization",
    "bearer",
    "cookie",
)

MASKED_VALUE = "***"


def _is_sensitive_field(name: str) -> bool:
    """True, wenn der Feldname auf einen Sensibel-Hint passt."""
    if not isinstance(name, str):
        return False
    needle = name.lower()
    return any(hint in needle for hint in SENSITIVE_FIELD_HINTS)


def mask_sensitive_fields(value: Any) -> Any:
    """Maskiert sensible Felder rekursiv in ``dict``/``list``-Strukturen.

    - Strings, Zahlen, Booleans und ``None`` werden unverändert
      durchgereicht (sie haben keinen Feldnamen-Kontext).
    - Bei ``dict``: Jeder Key wird auf ``SENSITIVE_FIELD_HINTS`` geprüft.
      Treffer → Wert wird zu ``"***"``. Sonst rekursiv weiter.
    - Bei ``list``/``tuple``: rekursiv pro Element.

    Liefert immer ein neues Objekt — die Eingabe wird nicht mutiert.
    """
    if isinstance(value, dict):
        masked: dict[Any, Any] = {}
        for key, sub in value.items():
            if _is_sensitive_field(key):
                masked[key] = MASKED_VALUE
            else:
                masked[key] = mask_sensitive_fields(sub)
        return masked

    if isinstance(value, list):
        return [mask_sensitive_fields(item) for item in value]

    if isinstance(value, tuple):
        return tuple(mask_sensitive_fields(item) for item in value)

    return value
