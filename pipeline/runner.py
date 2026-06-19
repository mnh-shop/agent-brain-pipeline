from __future__ import annotations

import asyncio
import logging
import traceback
from pathlib import Path
from typing import Any, Callable

from pipeline.config import get_config
from pipeline.db import claim_next_run, get_run, set_stage, update_run
from pipeline.stages import acquire, audit, curate, export, retrieval, semantics, structure, syntax, vector
from pipeline.util import utc_now, write_json

logger = logging.getLogger(__name__)

STAGE_FUNCTIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "acquire": acquire.run,
    "curate": curate.run,
    "syntax": syntax.run,
    "structure": structure.run,
    "semantics": semantics.run,
    "retrieval": retrieval.run,
    "vector": vector.run,
    "audit": audit.run,
    "export": export.run,
}


async def worker_loop(stop_event: asyncio.Event) -> None:
    cfg = get_config()
    poll = float(cfg["pipeline"].get("worker_poll_seconds", 3))
    while not stop_event.is_set():
        run = claim_next_run()
        if not run:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll)
            except asyncio.TimeoutError:
                continue
            continue
        await asyncio.to_thread(execute_run, run["run_id"])


def execute_run(run_id: str) -> None:
    cfg = get_config()
    try:
        for stage, stage_cfg in cfg["stages"].items():
            if stage not in STAGE_FUNCTIONS:
                continue
            run = get_run(run_id)
            if not run:
                raise RuntimeError(f"Run not found: {run_id}")
            owner = stage_cfg["owner_profile"]
            update_run(run_id, current_stage=stage)
            set_stage(run_id, stage, owner, "running")
            try:
                report = STAGE_FUNCTIONS[stage](run)
                report_path = _report_path(stage, get_run(run_id) or run, report)
                set_stage(run_id, stage, owner, "passed", report_path=report_path)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                set_stage(run_id, stage, owner, "failed", error=error)
                update_run(run_id, status="failed", error=error)
                try:
                    export.refresh_kanban()
                except Exception:
                    logger.exception("Could not refresh Kanban after failure")
                logger.exception("Run %s failed at stage %s", run_id, stage)
                return
        update_run(run_id, status="ready_for_wiki", current_stage="complete", completed_at=utc_now(), error=None)
        try:
            export.refresh_kanban()
        except Exception:
            logger.exception("Could not refresh Kanban after completion")
    except Exception as exc:
        update_run(run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        try:
            export.refresh_kanban()
        except Exception:
            logger.exception("Could not refresh Kanban after unhandled failure")
        logger.exception("Unhandled run failure for %s", run_id)


def _report_path(stage: str, run: dict[str, Any], report: dict[str, Any]) -> str | None:
    if stage == "acquire":
        snapshot = report.get("snapshot_path")
        return str(Path(snapshot).parent / "raw" / "source-manifest.json") if snapshot else None
    if stage == "export":
        return report.get("report_note")
    snapshot = run.get("snapshot_path")
    if not snapshot:
        return None
    mapping = {
        "curate": "curate-report.json",
        "syntax": "syntax-report.json",
        "structure": "codegraph-report.json",
        "semantics": "codebase-memory-report.json",
        "retrieval": "retrieval-report.json",
        "vector": "vector-report.json",
        "audit": "audit-report.json",
    }
    filename = mapping.get(stage)
    return str(Path(snapshot).parent / filename) if filename else None
