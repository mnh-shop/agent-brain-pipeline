from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from pipeline.config import get_config
from pipeline.db import create_run, get_run, initialize, list_runs, update_run
from pipeline.maintenance import scheduler_loop
from pipeline.runner import worker_loop
from pipeline.search import exact_search, fts_search, hybrid_search, semantic_search, structural_search, vector_search
from pipeline.stages import export
from pipeline.stages.audit import verify_run
from pipeline.urls import parse_repository_url
from pipeline.util import read_json

logging.basicConfig(level=getattr(logging, get_config().get("logging", {}).get("level", "INFO").upper(), logging.INFO))
logger = logging.getLogger(__name__)

stop_event = asyncio.Event()


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize()
    stop_event.clear()
    worker_task = asyncio.create_task(worker_loop(stop_event))
    scheduler_task = asyncio.create_task(scheduler_loop(stop_event))
    yield
    stop_event.set()
    await asyncio.gather(worker_task, scheduler_task, return_exceptions=True)


app = FastAPI(title="Agent Brain Pipeline", version="0.1.0", lifespan=lifespan)


class RunRequest(BaseModel):
    url: str
    ref: str | None = None
    trigger: str = "telegram"


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: Literal["exact", "fts", "vector", "structural", "semantic", "hybrid"] = "hybrid"
    source_id: str | None = None
    commit_sha: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


def authenticate(authorization: str | None = Header(default=None)) -> None:
    expected = str(get_config()["security"]["internal_api_token"])
    if not authorization or authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid API token")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/runs", dependencies=[Depends(authenticate)])
def submit_run(request: RunRequest) -> dict[str, Any]:
    repository = parse_repository_url(request.url)
    run_id = create_run(repository.normalized, request.ref, request.trigger)
    # The database remains authoritative; the Obsidian Kanban is refreshed
    # immediately so a Telegram-submitted repository is visible at once.
    try:
        export.refresh_kanban()
    except Exception:
        logger.exception("Could not refresh Kanban after queuing %s", run_id)
    return {"run_id": run_id, "status": "queued", "repository": repository.normalized}


@app.get("/runs", dependencies=[Depends(authenticate)])
def runs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return list_runs(limit)


@app.get("/runs/{run_id}", dependencies=[Depends(authenticate)])
def run_status(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    report_path = Path(str(run.get("snapshot_path", ""))).parent / "audit-report.json" if run.get("snapshot_path") else None
    quality_gate = None
    if report_path and report_path.exists():
        report = read_json(report_path)
        quality_gate = {
            "state": report.get("state"),
            "passed": report.get("passed"),
            "failed_checks": report.get("failed_checks", []),
        }
    return {**run, "quality_gate": quality_gate}


@app.get("/runs/{run_id}/reports/{stage}", dependencies=[Depends(authenticate)])
def stage_report(run_id: str, stage: str) -> Any:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    match = next((item for item in run.get("stages", []) if item.get("stage") == stage), None)
    if not match or not match.get("report_path"):
        raise HTTPException(status_code=404, detail="Stage report not available")
    path = Path(str(match["report_path"]))
    if path.suffix.lower() != ".json" or not path.exists():
        raise HTTPException(status_code=404, detail="Stage report is not a readable JSON report")
    return read_json(path)


@app.post("/runs/{run_id}/retry", dependencies=[Depends(authenticate)])
def retry_run(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    update_run(run_id, status="queued", current_stage=None, error=None, completed_at=None)
    return {"run_id": run_id, "status": "queued"}


@app.post("/runs/{run_id}/verify", dependencies=[Depends(authenticate)])
def verify_run_route(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    report = verify_run(run)
    return report


@app.post("/search", dependencies=[Depends(authenticate)])
def search(request: SearchRequest) -> Any:
    try:
        if request.mode == "exact":
            return exact_search(request.query, request.source_id, request.limit, request.commit_sha)
        if request.mode == "fts":
            return fts_search(request.query, request.source_id, request.limit, request.commit_sha)
        if request.mode == "vector":
            return vector_search(request.query, request.source_id, request.limit, request.commit_sha)
        if request.mode == "structural":
            return structural_search(request.query, request.source_id, request.limit, request.commit_sha)
        if request.mode == "semantic":
            return semantic_search(request.query, request.source_id, request.limit, request.commit_sha)
        return hybrid_search(request.query, request.source_id, request.limit, request.commit_sha)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def main() -> None:
    cfg = get_config()["api"]
    uvicorn.run("pipeline.api:app", host=cfg.get("host", "0.0.0.0"), port=int(cfg.get("port", 8080)), reload=False)


if __name__ == "__main__":
    main()
