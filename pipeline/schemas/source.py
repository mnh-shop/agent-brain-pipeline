from __future__ import annotations

from datetime import datetime

from pydantic import field_validator

from pipeline.schemas.base import CanonicalModel


class SourceRecord(CanonicalModel):
    source_id: str
    platform: str
    repository_url: str
    namespace: str
    repository_name: str
    requested_ref: str | None = None
    resolved_branch: str | None = None
    commit_sha: str | None = None
    default_branch: str | None = None
    latest_commit: str | None = None
    last_ingested_at: datetime | None = None
    next_refresh_at: datetime | None = None
    schema_version: int = 1
    pipeline_version: str

    @field_validator("platform")
    @classmethod
    def _platform(cls, value: str) -> str:
        if value not in {"github", "gitlab"}:
            raise ValueError("platform must be github or gitlab")
        return value


class SnapshotRecord(CanonicalModel):
    snapshot_id: str
    source_id: str
    platform: str
    repository_url: str
    namespace: str
    repository_name: str
    requested_ref: str | None = None
    resolved_branch: str | None = None
    commit_sha: str
    path: str
    raw_path: str | None = None
    bundle_path: str | None = None
    archive_path: str | None = None
    mirror_archive_path: str | None = None
    schema_version: int = 1
    pipeline_version: str
    source_line_start: int | None = None
    source_line_end: int | None = None
    source_byte_start: int | None = None
    source_byte_end: int | None = None
    file_sha256: str | None = None
    content_sha256: str | None = None
    generator_name: str = "acquire"
    generator_version: str = "1"
