from __future__ import annotations

from pydantic import Field

from pipeline.schemas.base import CanonicalModel


class ToolExecutionRecord(CanonicalModel):
    schema_version: int = Field(default=1, ge=1)
    pipeline_version: str
    execution_id: str
    run_id: str
    stage: str
    tool_name: str
    tool_version: str | None = None
    command: list[str]
    returncode: int
    started_at: str | None = None
    completed_at: str | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    metadata: dict[str, object] | None = None
