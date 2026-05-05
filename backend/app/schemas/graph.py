"""
Schemas fuer /api/graph/* Endpoints (Audit M6).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Konsistent mit utils/safe_id.py — proj_<8-32 hex>.
_PROJECT_ID_RE = re.compile(r"^proj_[a-f0-9]{8,32}$")


class OntologyGenerateRequest(BaseModel):
    """POST /api/graph/ontology/generate"""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=13, max_length=37)
    extra_instructions: str | None = Field(default=None, max_length=4000)

    @field_validator("project_id")
    @classmethod
    def _project_id_format(cls, v: str) -> str:
        if not _PROJECT_ID_RE.match(v):
            raise ValueError("project_id format invalid (expected proj_<hex>)")
        return v


class GraphBuildRequest(BaseModel):
    """POST /api/graph/build"""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=13, max_length=37)
    graph_name: str | None = Field(default=None, max_length=200)
    chunk_size: int | None = Field(default=None, ge=100, le=20000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=5000)
    force: bool = False

    @field_validator("project_id")
    @classmethod
    def _project_id_format(cls, v: str) -> str:
        if not _PROJECT_ID_RE.match(v):
            raise ValueError("project_id format invalid (expected proj_<hex>)")
        return v
