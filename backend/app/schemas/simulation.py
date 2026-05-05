"""
Schemas fuer /api/simulation/* Endpoints (Audit M6).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_PROJECT_ID_RE = re.compile(r"^proj_[a-f0-9]{8,32}$")
_SIM_ID_RE = re.compile(r"^sim_[a-f0-9]{8,32}$")


class SimulationCreateRequest(BaseModel):
    """POST /api/simulation/create"""

    model_config = ConfigDict(extra="ignore")

    project_id: str = Field(min_length=13, max_length=37)
    name: str | None = Field(default=None, max_length=200)
    platform: str | None = Field(default=None, max_length=20)
    max_rounds: int | None = Field(default=None, ge=1, le=1000)

    @field_validator("project_id")
    @classmethod
    def _project_id_format(cls, v: str) -> str:
        if not _PROJECT_ID_RE.match(v):
            raise ValueError("project_id format invalid (expected proj_<hex>)")
        return v

    @field_validator("platform")
    @classmethod
    def _platform_value(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in {"twitter", "reddit", "parallel"}:
            raise ValueError("platform must be twitter|reddit|parallel")
        return v


class SimulationStartRequest(BaseModel):
    """POST /api/simulation/start"""

    model_config = ConfigDict(extra="ignore")

    simulation_id: str = Field(min_length=12, max_length=36)

    @field_validator("simulation_id")
    @classmethod
    def _sim_id_format(cls, v: str) -> str:
        if not _SIM_ID_RE.match(v):
            raise ValueError("simulation_id format invalid (expected sim_<hex>)")
        return v
