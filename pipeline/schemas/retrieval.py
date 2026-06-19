from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class RetrievalResult(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    method: str
    source_id: str
    commit_sha: str
    run_id: str | None = None
    unit_id: str | None = None
    path: str | None = None
    score: float | None = None
    excerpt: str | None = None
    metadata: dict[str, object] | None = None
