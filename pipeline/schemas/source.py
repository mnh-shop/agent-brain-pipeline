from __future__ import annotations

from typing import Literal

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class SourceRecord(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    source_id: str
    platform: Literal["github", "gitlab"]
    repository_url: str
    namespace: str
    repository_name: str
    requested_ref: str | None = None
    resolved_branch: str | None = None
    commit_sha: str | None = None
    source_type: str = "repository"
    created_at: str | None = None
    updated_at: str | None = None


class SnapshotRecord(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    source_id: str
    platform: Literal["github", "gitlab"]
    repository_url: str
    namespace: str
    repository_name: str
    requested_ref: str | None = None
    resolved_branch: str | None = None
    commit_sha: str
    snapshot_path: str
    raw_path: str | None = None
    source_manifest_path: str | None = None
    created_by_run_id: str | None = None
    created_at: str | None = None
