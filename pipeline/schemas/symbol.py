from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class SymbolRecord(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    symbol_id: str
    source_id: str
    commit_sha: str
    run_id: str | None = None
    path: str
    language: str
    symbol_kind: str
    qualified_name: str
    start_byte: int | None = None
    end_byte: int | None = None
    start_line: int | None = None
    end_line: int | None = None
    content_sha256: str
    content: str
    parser_name: str | None = None
    parser_version: str | None = None
    unit_id: str | None = None
