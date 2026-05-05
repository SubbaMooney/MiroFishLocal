"""
Pydantic-Schemas fuer Body-Validation an API-System-Boundaries (Audit M6).

Jedes Schema vertritt EINEN Endpoint; es gibt bewusst keine Vererbungs-
Hierarchie, damit Aenderungen an einem Endpoint nicht versehentlich
andere Endpoints brechen (YAGNI ueber DRY).
"""

from .graph import GraphBuildRequest, OntologyGenerateRequest
from .report import ReportChatRequest, ReportGenerateRequest
from .simulation import SimulationCreateRequest, SimulationStartRequest

__all__ = [
    "GraphBuildRequest",
    "OntologyGenerateRequest",
    "ReportChatRequest",
    "ReportGenerateRequest",
    "SimulationCreateRequest",
    "SimulationStartRequest",
]
