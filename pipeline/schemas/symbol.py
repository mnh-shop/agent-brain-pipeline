from __future__ import annotations

from pipeline.schemas.base import CanonicalModel


class SymbolRecord(CanonicalModel):
    symbol_id: str
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
    language: str
    symbol_kind: str
    qualified_name: str
    start_byte: int | None = None
    end_byte: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    file_sha256: str
    content_sha256: str
    content: str | None = None
    schema_version: int = 1
    pipeline_version: str
    generator_name: str
    generator_version: str
    run_id: str | None = None
