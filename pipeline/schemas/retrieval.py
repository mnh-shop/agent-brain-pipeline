from __future__ import annotations

from pipeline.schemas.base import CanonicalModel


class RetrievalResult(CanonicalModel):
    query: str
    method: str
    source_id: str | None = None
    commit_sha: str | None = None
    run_id: str | None = None
    unit_id: str | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    excerpt: str | None = None
    score: float | None = None
    schema_version: int = 1
    pipeline_version: str
