"""Multi-tenant API schema (PLATFORM.md, section 7).

Every entity carries ``tenant_id``: designed multi-tenant from day one,
even though enforcement (real auth, quotas, billing) arrives progressively.
The persistence backend is pluggable (repo.py) -- these are just the shapes
the API stores and returns.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class GraphCreate(BaseModel):
    """Request body to store a graph spec."""

    name: str
    spec: dict[str, Any]


class Graph(BaseModel):
    """A stored graph spec, tenant-scoped and identified by id."""

    id: str
    tenant_id: str
    name: str
    spec: dict[str, Any]
    created_at: float


class RunStart(BaseModel):
    """Request body to run a stored graph over an input ref."""

    graph_id: str
    input_ref: str


class Run(BaseModel):
    """One run of a graph: its status, where it ended, and its time."""

    id: str
    tenant_id: str
    graph_id: str
    status: str
    final_ref: str | None
    created_at: float
    total_ms: float = 0.0


class UsageRecord(BaseModel):
    """One node's resource use in a run (RU input; timestamped)."""

    id: str
    tenant_id: str
    run_id: str
    node: str
    step: str
    disposition: str
    ms: float
    ts: float
