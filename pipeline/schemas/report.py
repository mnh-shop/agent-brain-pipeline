from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class StageReport(CanonicalModel):
    report_id: str
    run_id: str
    stage: str
    source_id: str | None = None
    commit_sha: str | None = None
    status: str
    passed: bool
    summary: dict[str, object] = Field(default_factory=dict)
    metrics: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[dict[str, object]] = Field(default_factory=list)
    schema_version: int = 1
    pipeline_version: str
