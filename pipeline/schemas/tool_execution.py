from __future__ import annotations

from pipeline.schemas.base import CanonicalModel


class ToolExecutionRecord(CanonicalModel):
    execution_id: str
    run_id: str
    stage: str
    tool_name: str
    tool_version: str | None = None
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    command: str | None = None
    input_json: str | None = None
    output_json: str | None = None
    error: str | None = None
    schema_version: int = 1
    pipeline_version: str
