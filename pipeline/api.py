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
from pipeline.util import read_json, utc_now, write_json

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


class WikiJobRequest(BaseModel):
    job_id: str | None = None
    notes: str | None = None


class WikiFailureRequest(WikiJobRequest):
    error: str = Field(min_length=1)


class WikiPageManifestRequest(WikiJobRequest):
    pages: list[dict[str, Any]] = Field(default_factory=list)
    manifest_name: str | None = None


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
        failures = report.get("failed_checks", [])
        quality_gate = {
            "state": report.get("state"),
            "passed": report.get("passed"),
            "failed_checks": failures,
            "failed_check_count": len(failures),
            "failed_check_names": [item.get("check") for item in failures if item.get("check")],
            "summary": "passed" if report.get("passed") else f"{len(failures)} failed checks",
        }
    return {**run, "quality_gate": quality_gate}


def _run_base(run: dict[str, Any]) -> Path:
    snapshot = run.get("snapshot_path")
    if not snapshot:
        raise HTTPException(status_code=404, detail="Run snapshot not available")
    return Path(snapshot).parent


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    return read_json(path) if path.exists() else None


def _deterministic_manifest(run: dict[str, Any]) -> dict[str, Any]:
    base = _run_base(run)
    audit_report = _read_optional_json(base / "audit-report.json")
    reproducibility = _read_optional_json(base / "reproducibility-report.json")
    artifact_manifest = _read_optional_json(base / "artifact-manifest.json")
    reports = {
        "integrity": _read_optional_json(base / "integrity-report.json"),
        "normalize": _read_optional_json(base / "normalization-report.json"),
        "lint": _read_optional_json(base / "lint-report.json"),
        "syntax": _read_optional_json(base / "syntax-report.json"),
        "structure": _read_optional_json(base / "codegraph-report.json"),
        "semantics": _read_optional_json(base / "codebase-memory-report.json"),
        "retrieval": _read_optional_json(base / "retrieval-report.json"),
        "vector": _read_optional_json(base / "vector-report.json"),
        "audit": audit_report,
    }
    return {
        "run_id": run["run_id"],
        "source_id": run.get("source_id"),
        "commit_sha": run.get("commit_sha"),
        "status": run.get("status"),
        "wiki_state": run.get("wiki_state"),
        "wiki_job_id": run.get("wiki_job_id"),
        "reports": reports,
        "artifact_manifest": artifact_manifest,
        "reproducibility": reproducibility,
        "quality_gate": audit_report.get("failed_checks", []) if audit_report else [],
    }


def _evidence_bundle(run: dict[str, Any]) -> dict[str, Any]:
    base = _run_base(run)
    return {
        "run_id": run["run_id"],
        "source_id": run.get("source_id"),
        "commit_sha": run.get("commit_sha"),
        "snapshot_path": run.get("snapshot_path"),
        "reports": {
            name: str(base / filename)
            for name, filename in {
                "integrity": "integrity-report.json",
                "normalize": "normalization-report.json",
                "lint": "lint-report.json",
                "syntax": "syntax-report.json",
                "structure": "codegraph-report.json",
                "semantics": "codebase-memory-report.json",
                "retrieval": "retrieval-report.json",
                "vector": "vector-report.json",
                "audit": "audit-report.json",
                "reproducibility": "reproducibility-report.json",
                "artifact_manifest": "artifact-manifest.json",
            }.items()
            if (base / filename).exists()
        },
        "wiki_state": run.get("wiki_state"),
        "wiki_job_id": run.get("wiki_job_id"),
        "wiki_manifest_path": run.get("wiki_manifest_path"),
        "wiki_evidence_path": run.get("wiki_evidence_path"),
        "wiki_page_manifest_path": run.get("wiki_page_manifest_path"),
    }


def _require_wiki_ready(run: dict[str, Any]) -> None:
    if run.get("status") != "ready_for_wiki":
        raise HTTPException(status_code=409, detail="Run is not ready_for_wiki")


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


@app.get("/runs/{run_id}/wiki/manifest", dependencies=[Depends(authenticate)])
def get_deterministic_run_manifest(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return _deterministic_manifest(run)


@app.get("/runs/{run_id}/wiki/evidence", dependencies=[Depends(authenticate)])
def get_evidence_bundle(run_id: str) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return _evidence_bundle(run)


@app.post("/runs/{run_id}/wiki/started", dependencies=[Depends(authenticate)])
def report_wiki_job_started(run_id: str, request: WikiJobRequest) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    _require_wiki_ready(run)
    update_run(run_id, wiki_state="wiki_running", wiki_job_id=request.job_id, wiki_started_at=utc_now(), wiki_error=None)
    return {"run_id": run_id, "wiki_state": "wiki_running", "job_id": request.job_id}


@app.post("/runs/{run_id}/wiki/completed", dependencies=[Depends(authenticate)])
def report_wiki_job_completed(run_id: str, request: WikiJobRequest) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("wiki_state") not in {"wiki_running", "wiki_generated"}:
        raise HTTPException(status_code=409, detail="Wiki job has not started")
    update_run(run_id, wiki_state="wiki_generated", wiki_job_id=request.job_id, wiki_completed_at=utc_now(), wiki_error=None)
    return {"run_id": run_id, "wiki_state": "wiki_generated", "job_id": request.job_id}


@app.post("/runs/{run_id}/wiki/failed", dependencies=[Depends(authenticate)])
def report_wiki_job_failed(run_id: str, request: WikiFailureRequest) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("wiki_state") not in {"wiki_running", "wiki_generated"}:
        raise HTTPException(status_code=409, detail="Wiki job has not started")
    update_run(run_id, wiki_state="wiki_validation_failed", wiki_job_id=request.job_id, wiki_failed_at=utc_now(), wiki_error=request.error)
    return {"run_id": run_id, "wiki_state": "wiki_validation_failed", "job_id": request.job_id, "error": request.error}


@app.post("/runs/{run_id}/wiki/page-manifest", dependencies=[Depends(authenticate)])
def submit_generated_page_manifest(run_id: str, request: WikiPageManifestRequest) -> dict[str, Any]:
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.get("wiki_state") not in {"wiki_generated", "wiki_running"}:
        raise HTTPException(status_code=409, detail="Wiki job has not produced pages")
    wiki_cfg = get_config().get("wiki", {})
    candidate_path = Path(str(wiki_cfg.get("candidate_path", "/vault/wiki/candidates")))
    candidate_path.mkdir(parents=True, exist_ok=True)
    for page in request.pages:
        page_path = page.get("path")
        if page_path:
            resolved = Path(str(page_path)).resolve()
            if candidate_path.resolve() not in resolved.parents and resolved != candidate_path.resolve():
                update_run(run_id, wiki_state="wiki_validation_failed", wiki_error="page manifest must stay under candidate_path")
                raise HTTPException(status_code=400, detail="Page manifest must stay under candidate_path")
    manifest_path = candidate_path / f"{run_id}.page-manifest.json"
    manifest = {
        "run_id": run_id,
        "source_id": run.get("source_id"),
        "commit_sha": run.get("commit_sha"),
        "wiki_state": "ready_for_review",
        "job_id": request.job_id,
        "pages": request.pages,
        "manifest_name": request.manifest_name or manifest_path.name,
    }
    write_json(manifest_path, manifest)
    update_run(
        run_id,
        wiki_state="ready_for_review",
        wiki_job_id=request.job_id,
        wiki_page_manifest_path=str(manifest_path),
        wiki_manifest_path=str(manifest_path),
        wiki_error=None,
    )
    return {"run_id": run_id, "wiki_state": "ready_for_review", "manifest_path": str(manifest_path), "page_count": len(request.pages)}


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
