from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.db import connect, record_stage_report
from pipeline.util import write_json


def run(run: dict[str, Any]) -> dict[str, Any]:
    with connect() as connection:
        unit_count = connection.execute("SELECT count(*) FROM units WHERE run_id=?", (run["run_id"],)).fetchone()[0]
        fts_count = connection.execute(
            "SELECT count(*) FROM units_fts WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()[0]
        smoke = connection.execute(
            "SELECT unit_id,path FROM units WHERE run_id=? ORDER BY path LIMIT 1", (run["run_id"],)
        ).fetchone()
    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "unit_count": unit_count,
        "fts_count": fts_count,
        "smoke_result": dict(smoke) if smoke else None,
        "methods": ["exact", "fts", "structural", "semantic", "hybrid"],
        "passed": unit_count > 0 and fts_count == unit_count,
    }
    report_path = Path(run["snapshot_path"]).parent / "retrieval-report.json"
    write_json(report_path, report)
    record_stage_report({
        "run_id": run["run_id"],
        "stage": "retrieval",
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "status": "passed" if report["passed"] else "failed",
        "passed": report["passed"],
        "summary": {"unit_count": unit_count, "fts_count": fts_count},
        "metrics": report,
        "warnings": [],
        "errors": [],
        "schema_version": 1,
        "pipeline_version": "0.1.0",
    })
    if not report["passed"]:
        raise RuntimeError(f"Retrieval index validation failed; see {report_path}")
    return report
