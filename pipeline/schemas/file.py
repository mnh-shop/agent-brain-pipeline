from __future__ import annotations

from pipeline.schemas.base import CanonicalModel


class FileRecord(CanonicalModel):
    source_id: str
    platform: str
    repository_url: str
    namespace: str
    repository_name: str
    requested_ref: str | None = None
    resolved_branch: str | None = None
    commit_sha: str
    path: str
    file_sha256: str
    content_sha256: str | None = None
    source_line_start: int | None = None
    source_line_end: int | None = None
    source_byte_start: int | None = None
    source_byte_end: int | None = None
    mime_type: str | None = None
    encoding: str | None = None
    is_binary: bool = False
    duplicate_of: str | None = None
    schema_version: int = 1
    pipeline_version: str
    generator_name: str = "curate"
    generator_version: str = "1"
