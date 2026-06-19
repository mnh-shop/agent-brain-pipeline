from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException

from pipeline.config import get_config
from pipeline.db import create_run, get_run, initialize, update_run
from pipeline.urls import parse_repository_url


def _prepare_config(tmp_path: Path) -> Path:
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["storage"]["data_dir"] = str(tmp_path / "data")
    cfg["storage"]["obsidian_dir"] = str(tmp_path / "vault")
    cfg["wiki"]["vault_path"] = str(tmp_path / "vault")
    cfg["wiki"]["raw_path"] = str(tmp_path / "vault" / "raw")
    cfg["wiki"]["wiki_path"] = str(tmp_path / "vault" / "wiki")
    cfg["wiki"]["candidate_path"] = str(tmp_path / "vault" / "wiki" / "candidates")
    cfg["wiki"]["canonical_path"] = str(tmp_path / "vault" / "wiki" / "canonical")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    os.environ["AGENT_BRAIN_CONFIG"] = str(config_path)
    get_config.cache_clear()
    return config_path


def _seed_reports(base: Path, source_id: str = "source") -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "integrity-report.json").write_text('{"passed": true}', encoding="utf-8")
    (base / "normalization-report.json").write_text('{"passed": true}', encoding="utf-8")
    (base / "lint-report.json").write_text('{"passed": true}', encoding="utf-8")
    (base / "syntax-report.json").write_text('{"passed": true}', encoding="utf-8")
    (base / "codegraph-report.json").write_text('{"commit_sha": "deadbeef", "passed": true}', encoding="utf-8")
    (base / "codebase-memory-report.json").write_text('{"commit_sha": "deadbeef", "passed": true}', encoding="utf-8")
    (base / "retrieval-report.json").write_text('{"passed": true}', encoding="utf-8")
    (base / "vector-report.json").write_text('{"passed": true, "indexed_vector_count": 1, "eligible_unit_count": 1, "vector_dimensions": 384, "model_revision": "v1", "embedding_model": "agent-brain-local-hash-embedding", "model_cache_path": "/tmp/model", "metadata_filter_ok": true, "smoke_results": [{"unit_id": "unit-1", "vector_dimensions": 384, "source_id": "source", "commit_sha": "deadbeef", "content": "hello"}], "index_path": "/tmp/index", "table": "units", "metric": "cosine"}', encoding="utf-8")
    (base / "audit-report.json").write_text('{"state": "ready_for_wiki", "passed": true, "failed_checks": []}', encoding="utf-8")
    (base / "reproducibility-report.json").write_text('{"passed": true, "current_sha256": "a", "replay_sha256": "a"}', encoding="utf-8")
    (base / "artifact-manifest.json").write_text(f'{{"run_id": "RUN-1", "source_id": "{source_id}", "commit_sha": "deadbeef", "files": [], "schema_version": 1}}', encoding="utf-8")


def _seed_source(tmp_path: Path, repo_url: str, commit_sha: str) -> None:
    repo = parse_repository_url(repo_url)
    db = tmp_path / "data" / "pipeline.sqlite"
    now = "2026-06-19T00:00:00Z"
    with sqlite3.connect(db) as connection:
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
            (repo.source_id, repo.platform, repo.normalized, repo.namespace, repo.name, repo.name, "main", commit_sha, now, now, now, now),
        )


def test_knowledge_writer_profile_renders_official_skills(tmp_path: Path) -> None:
    _prepare_config(tmp_path)
    initialize()
    repo_url = "https://github.com/example/repo.git"
    repo = parse_repository_url(repo_url)
    run_id = create_run(repo_url, "main", "manual")
    _seed_source(tmp_path, repo_url, "deadbeef")
    base = tmp_path / "data" / "derived" / repo.source_id / "deadbeef"
    snapshot = base / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    update_run(run_id, snapshot_path=str(snapshot), source_id=repo.source_id, commit_sha="deadbeef", status="ready_for_wiki", current_stage="complete")
    _seed_reports(base, repo.source_id)

    api = importlib.import_module("pipeline.api")
    manifest = api.get_deterministic_run_manifest(run_id)
    evidence = api.get_evidence_bundle(run_id)
    assert manifest["wiki_state"] is None or manifest["wiki_state"] == "awaiting_wiki_agent"
    assert "audit" in manifest["reports"]
    assert evidence["reports"]["audit"].endswith("audit-report.json")


def test_knowledge_writer_state_transitions_and_manifest_submission(tmp_path: Path) -> None:
    _prepare_config(tmp_path)
    initialize()
    repo_url = "https://github.com/example/repo.git"
    repo = parse_repository_url(repo_url)
    run_id = create_run(repo_url, "main", "manual")
    _seed_source(tmp_path, repo_url, "deadbeef")
    base = tmp_path / "data" / "derived" / repo.source_id / "deadbeef"
    snapshot = base / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    update_run(run_id, snapshot_path=str(snapshot), source_id=repo.source_id, commit_sha="deadbeef", status="ready_for_wiki", current_stage="complete")
    _seed_reports(base, repo.source_id)

    api = importlib.import_module("pipeline.api")
    started = api.report_wiki_job_started(run_id, api.WikiJobRequest(job_id="job-1"))
    assert started["wiki_state"] == "wiki_running"
    assert get_run(run_id)["wiki_state"] == "wiki_running"

    completed = api.report_wiki_job_completed(run_id, api.WikiJobRequest(job_id="job-1"))
    assert completed["wiki_state"] == "wiki_generated"
    assert get_run(run_id)["wiki_state"] == "wiki_generated"

    candidate_path = Path(get_config()["wiki"]["candidate_path"])
    candidate_path.mkdir(parents=True, exist_ok=True)
    response = api.submit_generated_page_manifest(
        run_id,
        api.WikiPageManifestRequest(
            job_id="job-1",
            pages=[{"path": str(candidate_path / "page.md"), "title": "Page"}],
            manifest_name="page-manifest.json",
        ),
    )
    assert response["wiki_state"] == "ready_for_review"
    assert get_run(run_id)["wiki_state"] == "ready_for_review"
    assert Path(response["manifest_path"]).exists()


def test_knowledge_writer_manifest_rejects_non_candidate_pages(tmp_path: Path) -> None:
    _prepare_config(tmp_path)
    initialize()
    repo_url = "https://github.com/example/repo.git"
    repo = parse_repository_url(repo_url)
    run_id = create_run(repo_url, "main", "manual")
    _seed_source(tmp_path, repo_url, "deadbeef")
    base = tmp_path / "data" / "derived" / repo.source_id / "deadbeef"
    snapshot = base / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    update_run(run_id, snapshot_path=str(snapshot), source_id=repo.source_id, commit_sha="deadbeef", status="ready_for_wiki", current_stage="complete")
    _seed_reports(base, repo.source_id)

    api = importlib.import_module("pipeline.api")
    api.report_wiki_job_started(run_id, api.WikiJobRequest(job_id="job-1"))
    api.report_wiki_job_completed(run_id, api.WikiJobRequest(job_id="job-1"))
    bad_page = Path(tmp_path / "outside.md")
    with pytest.raises(HTTPException):
        api.submit_generated_page_manifest(
            run_id,
            api.WikiPageManifestRequest(
                job_id="job-1",
                pages=[{"path": str(bad_page), "title": "Page"}],
                manifest_name="page-manifest.json",
            ),
        )
    assert get_run(run_id)["wiki_state"] == "wiki_validation_failed"
