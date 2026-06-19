from __future__ import annotations

import os
import importlib
from pathlib import Path
import sqlite3

import pytest
import yaml

from pipeline.config import get_config
from pipeline.db import create_run, initialize, update_run
from pipeline.stages import audit
from pipeline.stages._audit import ValidationResult
from pipeline.search import _latest_completed_runs
from pipeline.urls import parse_repository_url


def _prepare_config(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["storage"]["data_dir"] = str(tmp_path / "data")
    cfg["storage"]["obsidian_dir"] = str(tmp_path / "vault")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    os.environ["AGENT_BRAIN_CONFIG"] = str(config_path)
    get_config.cache_clear()


def test_audit_report_tracks_failed_checks_and_ready_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_config(tmp_path)
    run = {"run_id": "RUN-1", "source_id": "source", "commit_sha": "deadbeef", "snapshot_path": str(tmp_path / "snapshot")}
    (tmp_path / "snapshot").mkdir()

    monkeypatch.setattr(audit, "validate_source_integrity", lambda run: [ValidationResult("source", True, {"ok": True})])
    monkeypatch.setattr(audit, "validate_units_and_symbols", lambda run: [ValidationResult("units", True, {"ok": True})])
    monkeypatch.setattr(audit, "validate_graph_integrity", lambda run: [ValidationResult("graph", True, {"ok": True})])
    monkeypatch.setattr(audit, "validate_vector_integrity", lambda run: [ValidationResult("vector", True, {"ok": True})])
    monkeypatch.setattr(audit, "validate_markdown_exports", lambda run, artifact_manifest=None: [ValidationResult("markdown", False, {"ok": False})])
    monkeypatch.setattr(audit, "validate_retrieval_integrity", lambda run: [ValidationResult("retrieval", True, {"ok": True})])
    monkeypatch.setattr(audit, "compare_reproducibility", lambda run: {"schema_version": 1, "passed": True, "current_sha256": "a", "replay_sha256": "a", "manifest": {"run_id": "RUN-1", "source_id": "source", "commit_sha": "deadbeef", "files": [], "schema_version": 1}})
    monkeypatch.setattr(audit, "build_artifact_manifest", lambda run: {"run_id": "RUN-1", "source_id": "source", "commit_sha": "deadbeef", "files": [], "schema_version": 1})

    report = audit._report(run)
    assert report["state"] == "failed"
    assert report["passed"] is False
    assert any(item["group"] == "markdown" for item in report["failed_checks"])


def test_run_status_and_verify_route_expose_quality_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_config(tmp_path)
    api = importlib.import_module("pipeline.api")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    audit_report = {
        "state": "failed",
        "passed": False,
        "failed_checks": [{"group": "vector", "check": "vector_count_matches", "passed": False}],
    }
    (tmp_path / "audit-report.json").write_text(__import__("json").dumps(audit_report), encoding="utf-8")
    run = {"run_id": "RUN-1", "snapshot_path": str(snapshot), "status": "failed"}
    monkeypatch.setattr(api, "get_run", lambda run_id: run)
    monkeypatch.setattr(api, "verify_run", lambda run: {"state": "ready_for_wiki", "passed": True})

    status = api.run_status("RUN-1")
    assert status["quality_gate"]["state"] == "failed"
    assert status["quality_gate"]["failed_checks"][0]["group"] == "vector"

    verified = api.verify_run_route("RUN-1")
    assert verified["passed"] is True


def test_latest_completed_runs_accepts_ready_for_wiki_status(tmp_path: Path) -> None:
    _prepare_config(tmp_path)
    initialize()
    repo_url = "https://github.com/example/repo.git"
    run_id = create_run(repo_url, "main", "manual")
    repo = parse_repository_url(repo_url)
    with sqlite3.connect(Path(tmp_path / "data" / "pipeline.sqlite")) as connection:
        connection.execute(
            """
            INSERT INTO sources(source_id,platform,repository_url,namespace,name,repository_name,default_branch,latest_commit,last_ingested_at,next_refresh_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
              repository_url=excluded.repository_url,
              default_branch=excluded.default_branch,
              latest_commit=excluded.latest_commit,
              last_ingested_at=excluded.last_ingested_at,
              next_refresh_at=excluded.next_refresh_at,
              updated_at=excluded.updated_at
            """,
            (
                repo.source_id,
                repo.platform,
                repo.normalized,
                repo.namespace,
                repo.name,
                repo.name,
                "main",
                "deadbeef",
                "2026-06-19T00:00:00Z",
                "2026-06-19T00:00:00Z",
                "2026-06-19T00:00:00Z",
                "2026-06-19T00:00:00Z",
            ),
        )
    update_run(run_id, source_id=repo.source_id, commit_sha="deadbeef", resolved_branch="main", status="ready_for_wiki", completed_at="2026-06-19T00:00:00Z")

    runs = _latest_completed_runs(repo.source_id, "deadbeef")
    assert runs
    assert runs[0]["status"] == "ready_for_wiki"
