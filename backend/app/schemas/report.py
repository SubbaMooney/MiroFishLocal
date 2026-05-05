"""
Schemas fuer /api/report/* Endpoints (Audit M6).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SIM_ID_RE = re.compile(r"^sim_[a-f0-9]{8,32}$")


class ReportGenerateRequest(BaseModel):
    """POST /api/report/generate"""

    model_config = ConfigDict(extra="forbid")

    simulation_id: str = Field(min_length=12, max_length=36)
    force_regenerate: bool = False

    @field_validator("simulation_id")
    @classmethod
    def _sim_id_format(cls, v: str) -> str:
        if not _SIM_ID_RE.match(v):
            raise ValueError("simulation_id format invalid (expected sim_<hex>)")
        return v


class ReportChatRequest(BaseModel):
    """POST /api/report/chat (H4 — server-side history; Client schickt nur message + simulation_id)."""

    model_config = ConfigDict(extra="ignore")  # Bewusst ignore: chat_history aus alten Clients darf nicht 400 werfen, wird einfach verworfen.

    simulation_id: str = Field(min_length=12, max_length=36)
    message: str = Field(min_length=1, max_length=10_000)

    @field_validator("simulation_id")
    @classmethod
    def _sim_id_format(cls, v: str) -> str:
        if not _SIM_ID_RE.match(v):
            raise ValueError("simulation_id format invalid (expected sim_<hex>)")
        return v
