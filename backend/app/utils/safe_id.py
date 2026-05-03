"""
Pfad- und ID-Validatoren gegen Path-Traversal (Audit-Finding C6).

Drei Funktionen:

- ``safe_id(value, prefix=None)`` validiert User-gelieferte IDs gegen ein
  whitelisted Format ``^(proj|sim|report|task)_[a-f0-9]{8,32}$``. Gibt die
  Original-ID zurueck oder wirft ``ValueError``.

- ``safe_path_under(base, *parts)`` baut einen Pfad und stellt per
  ``os.path.realpath`` sicher, dass er innerhalb von ``base`` bleibt. Wirft
  ``ValueError`` bei Traversal (``..``, absoluten Komponenten, Symlink-Escape).

- ``safe_filename(value, allowed_ext=None)`` validiert einen einfachen
  Dateinamen ohne Pfadtrenner und optional gegen Extension-Whitelist
  (z. B. fuer ``simulation_config.json``-Downloads).

Diese Util ist Single-Source-of-Truth — alle ID-/Pfad-bezogenen Routes
muessen sie verwenden statt Strings direkt in ``os.path.join`` zu reichen.
"""

from __future__ import annotations

import os
import re
from typing import Iterable, Optional

# Format-Whitelist: nur unsere eigenen ID-Praefixe + Hex-Suffix.
# Beispiele: proj_abc12345, sim_a1b2c3d4e5f67890, report_deadbeefcafebabe
_ID_RE = re.compile(r'^(proj|sim|report|task)_[a-f0-9]{8,32}$')

# Erlaubte ID-Praefixe.
_ALLOWED_PREFIXES = ('proj', 'sim', 'report', 'task')


def safe_id(value: str, prefix: Optional[str] = None) -> str:
    """Validiert eine User-gelieferte ID gegen das Whitelist-Format.

    Args:
        value: Die zu pruefende ID (z. B. ``proj_abc12345``).
        prefix: Optional — wenn gesetzt, muss die ID mit diesem Praefix
            (gefolgt von ``_``) beginnen. Erlaubt sind nur Werte aus
            ``_ALLOWED_PREFIXES``.

    Returns:
        Die unveraenderte ID (durchgereicht), wenn sie valide ist.

    Raises:
        ValueError: Bei ungueltigem Format oder falschem Praefix.
    """
    if not isinstance(value, str):
        raise ValueError("id muss ein String sein")
    if not value:
        raise ValueError("id darf nicht leer sein")
    if not _ID_RE.match(value):
        raise ValueError("id hat ungueltiges Format")
    if prefix is not None:
        if prefix not in _ALLOWED_PREFIXES:
            raise ValueError(f"unbekanntes id-prefix: {prefix}")
        if not value.startswith(prefix + "_"):
            raise ValueError(f"id muss mit '{prefix}_' beginnen")
    return value


def safe_path_under(base: str, *parts: str) -> str:
    """Baut einen Pfad unter ``base`` und prueft auf Traversal-Escape.

    Verbindet ``base`` mit allen ``parts`` per ``os.path.join``, resolved den
    finalen Pfad per ``os.path.realpath`` (folgt Symlinks) und vergleicht ihn
    mit dem realen ``base``. Liegt der finale Pfad nicht unter ``base``,
    wird ``ValueError`` geworfen.

    Args:
        base: Erlaubter Root-Ordner (absoluter oder relativer Pfad).
        *parts: User-Komponenten, die unter ``base`` landen sollen.

    Returns:
        Den realen, validierten Pfad als String.

    Raises:
        ValueError: Bei Path-Traversal (``..``, absolute Pfade, Symlink-Escape).
    """
    if not parts:
        raise ValueError("safe_path_under braucht mindestens eine Pfad-Komponente")

    real_base = os.path.realpath(base)
    joined = os.path.join(base, *parts)
    real_target = os.path.realpath(joined)

    # commonpath wirft, wenn die Pfade auf unterschiedlichen Drives liegen
    # (Windows). In dem Fall ist es per Definition kein Sub-Pfad.
    try:
        common = os.path.commonpath([real_base, real_target])
    except ValueError as exc:
        raise ValueError("Pfad liegt nicht unter dem erlaubten Root") from exc

    if common != real_base:
        raise ValueError("Pfad liegt nicht unter dem erlaubten Root")

    return real_target


# Erlaubte Zeichen fuer einfache Dateinamen (z. B. download-Whitelists).
_FILENAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def safe_filename(value: str, allowed_ext: Optional[Iterable[str]] = None) -> str:
    """Validiert einen einfachen Dateinamen ohne Pfadtrenner.

    Args:
        value: Dateiname (kein Pfad-Separator erlaubt).
        allowed_ext: Optional — Liste erlaubter Extensions (z. B. ``["json"]``).

    Returns:
        Den Dateinamen unveraendert.

    Raises:
        ValueError: Bei ungueltigen Zeichen, Pfad-Separator oder unerlaubter Extension.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("filename muss ein nicht-leerer String sein")
    if os.sep in value or (os.altsep and os.altsep in value):
        raise ValueError("filename darf keinen Pfad-Separator enthalten")
    if not _FILENAME_RE.match(value):
        raise ValueError("filename enthaelt ungueltige Zeichen")
    if allowed_ext is not None:
        ext = os.path.splitext(value)[1].lstrip('.').lower()
        allowed_lower = {e.lstrip('.').lower() for e in allowed_ext}
        if ext not in allowed_lower:
            raise ValueError(f"filename-extension '{ext}' nicht erlaubt")
    return value
