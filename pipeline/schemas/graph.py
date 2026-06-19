from __future__ import annotations

from pipeline.schemas.base import CanonicalModel


class GraphNode(CanonicalModel):
    node_id: str
    label: str
    kind: str
    source_id: str
    commit_sha: str
    path: str | None = None
    schema_version: int = 1
    pipeline_version: str


class GraphEdge(CanonicalModel):
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: str
    source_id: str
    commit_sha: str
    path: str | None = None
    schema_version: int = 1
    pipeline_version: str
