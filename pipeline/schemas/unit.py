from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class UnitRecord(CanonicalModel):
    unit_id: str
    source_id: str
    platform: str
    repository_url: str
    namespace: str
    repository_name: str
    requested_ref: str | None = None
    resolved_branch: str | None = None
    commit_sha: str
    path: str
    normalized_path: str
    unit_type: str
    heading: str | None = None
    language: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    start_byte: int | None = None
    end_byte: int | None = None
    file_sha256: str
    content_sha256: str
    content: str
    schema_version: int = 1
    pipeline_version: str
    generator_name: str
    generator_version: str
    source_line_start: int | None = None
    source_line_end: int | None = None
    source_byte_start: int | None = None
    source_byte_end: int | None = None
    run_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
