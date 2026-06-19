from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class GraphNode(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    node_id: str
    source_id: str
    commit_sha: str
    node_type: str
    label: str
    path: str | None = None
    qualified_name: str | None = None
    payload: dict[str, object] | None = None


class GraphEdge(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    edge_id: str
    source_id: str
    commit_sha: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    payload: dict[str, object] | None = None
