"""
Resource-Ownership-Decorator gegen Enumeration-Angriffe (Audit-Finding H3).

MiroFish ist ein Single-User-System (siehe ``CLAUDE.md``). H3 ist daher
KEIN Multi-Tenant-ACL-Mechanismus, sondern ein Enumeration-Schutz: ein
syntaktisch valider, aber nicht in der lokalen Resource-Registry
registrierter Bezeichner darf keine Information ueber das Datenmodell
preisgeben und keine Aktion ausloesen.

Der Decorator ``@require_resource(kind, id_param)``:

1. Validiert das ID-Format per ``safe_id()`` (existiert seit C6).
2. Schlaegt Existenz in der zustaendigen Registry nach
   (``ProjectManager`` / ``SimulationManager`` / ``ReportManager``).
3. Antwortet mit ``404`` ohne Details, wenn die Resource nicht
   registriert ist. Die ``404``-Wahl folgt dem etablierten Muster
   in den existierenden Routen (``api.simulationNotFound`` etc.).

Erlaubt sind die Resource-Typen:

- ``"project"`` -- IDs ``proj_<hex>``, registriert via ``ProjectManager``.
- ``"simulation"`` -- IDs ``sim_<hex>``, registriert via ``SimulationManager``.
- ``"report"`` -- IDs ``report_<hex>``, registriert via ``ReportManager``.
- ``"graph"`` -- freie ID-Strings; gueltig nur, wenn mindestens ein Projekt
  diese ``graph_id`` traegt. ``graph_id`` folgt nicht dem ``safe_id``-
  Format (LightRAG-Bezeichner sind frei waehlbar) und wird daher nur
  gegen die Projekt-Registry gepruet, nicht gegen ``_ID_RE``.

Beispiel::

    @simulation_bp.route('/<simulation_id>/posts')
    @require_resource('simulation', 'simulation_id')
    def get_simulation_posts(simulation_id):
        ...

Der Decorator stellt sicher, dass ``simulation_id`` registriert ist,
bevor der Handler ueberhaupt laeuft.
"""

from __future__ import annotations

import re
from functools import wraps
from typing import Callable

from flask import jsonify, request

from .safe_id import safe_id

# Whitelist gueltiger Resource-Typen.
_VALID_KINDS = ('project', 'simulation', 'report', 'graph')

# Praefix-Mapping fuer ``safe_id``-Validierung.
# ``graph`` ist absichtlich nicht enthalten -- siehe Modul-Docstring.
_KIND_TO_PREFIX = {
    'project': 'proj',
    'simulation': 'sim',
    'report': 'report',
}

# Liberales Format fuer graph_ids (LightRAG-Bezeichner). Wir erlauben
# alphanumerisch + ``-_``, 1-128 Zeichen. Verhindert Pfad-Separatoren,
# Whitespace und Steuerzeichen, ohne legitime IDs abzulehnen.
_GRAPH_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def _resource_exists(kind: str, resource_id: str) -> bool:
    """Lookup gegen die zustaendige Registry.

    Imports stehen lazy in der Funktion, damit das Auth-Util keinen
    Import-Cycle gegen ``models.project`` / ``services.*`` triggert.
    """
    if kind == 'project':
        from ..models.project import ProjectManager
        return ProjectManager.get_project(resource_id) is not None

    if kind == 'simulation':
        from ..services.simulation_manager import SimulationManager
        return SimulationManager().get_simulation(resource_id) is not None

    if kind == 'report':
        from ..services.report_agent import ReportManager
        return ReportManager.get_report(resource_id) is not None

    if kind == 'graph':
        # graph_id ist gueltig, wenn ein Projekt sie als ``graph_id`` traegt.
        from ..models.project import ProjectManager
        for project in ProjectManager.list_projects(limit=10000):
            if project.graph_id == resource_id:
                return True
        return False

    raise ValueError(f"unbekannter resource-typ: {kind}")


def require_resource(kind: str, id_param: str) -> Callable:
    """Decorator-Factory: validiert ID-Format und Registrierung.

    Args:
        kind: Einer von ``project``, ``simulation``, ``report``, ``graph``.
        id_param: Name des URL-/View-Parameters, der die Resource-ID traegt.

    Returns:
        Den umhuellten View-Handler. Bei ungueltigem Format oder
        nicht-registrierter ID antwortet er mit ``404`` und einer
        generischen Fehlermeldung.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"unbekannter resource-typ: {kind}")

    def decorator(view_func: Callable) -> Callable:
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            resource_id = kwargs.get(id_param)
            if resource_id is None and request.view_args:
                resource_id = request.view_args.get(id_param)

            if not resource_id:
                # Programmierfehler: Decorator falsch verdrahtet.
                # Wir antworten 404 statt 500, damit der Fehlerpfad
                # keine internen Details leakt.
                return jsonify({
                    "success": False,
                    "error": "Resource not found",
                }), 404

            # Format-Validierung.
            try:
                if kind == 'graph':
                    if not isinstance(resource_id, str) or not _GRAPH_ID_RE.match(resource_id):
                        raise ValueError("graph_id hat ungueltiges Format")
                else:
                    safe_id(resource_id, prefix=_KIND_TO_PREFIX[kind])
            except ValueError:
                return jsonify({
                    "success": False,
                    "error": "Resource not found",
                }), 404

            # Registry-Lookup.
            if not _resource_exists(kind, resource_id):
                return jsonify({
                    "success": False,
                    "error": "Resource not found",
                }), 404

            return view_func(*args, **kwargs)

        return wrapper

    return decorator
