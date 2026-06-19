from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class StageReport(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    run_id: str
    stage: str
    source_id: str | None = None
    commit_sha: str | None = None
    passed: bool
    summary: str | None = None
    payload: dict[str, object]
