from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class FileRecord(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    file_id: str
    source_id: str
    commit_sha: str
    run_id: str | None = None
    path: str
    file_sha256: str
    size_bytes: int
    mime_type: str | None = None
    encoding: str | None = None
    is_binary: bool = False
    duplicate_of: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
