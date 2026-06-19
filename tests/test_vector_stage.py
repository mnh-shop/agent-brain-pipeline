from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from pipeline.config import get_config
from pipeline.db import create_run, get_run, initialize, update_run
from pipeline.embeddings.base import EmbeddingBackendInfo
from pipeline.search import vector_search
from pipeline.stages import syntax, vector
from pipeline.urls import parse_repository_url
from pipeline.util import utc_now


class FakeEmbeddingBackend:
    def __init__(self, model: str = "fake-hash-embedding", revision: str = "test", dimensions: int = 32, normalize: bool = True) -> None:
        self._info = EmbeddingBackendInfo(
            provider="local",
            model=model,
            revision=revision,
            dimensions=dimensions,
            normalize=normalize,
        )

    @property
    def info(self) -> EmbeddingBackendInfo:
        return self._info

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._info.dimensions
            for index, char in enumerate(text.lower()):
                vector[(index + ord(char)) % self._info.dimensions] += 1.0
            total = sum(value * value for value in vector) ** 0.5
            if total:
                vector = [value / total for value in vector]
            vectors.append(vector)
        return vectors


def _prepare_config(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["storage"]["data_dir"] = str(tmp_path / "data")
    cfg["storage"]["obsidian_dir"] = str(tmp_path / "vault")
    cfg["lancedb"]["path"] = str(tmp_path / "indexes" / "lancedb")
    cfg["embeddings"]["dimensions"] = 32
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    os.environ["AGENT_BRAIN_CONFIG"] = str(config_path)
    get_config.cache_clear()


def _fixture_snapshot(tmp_path: Path) -> Path:
    fixture = Path(__file__).parents[1] / "tests" / "fixtures" / "syntax_repo"
    snapshot = tmp_path / "snapshot"
    shutil.copytree(fixture, snapshot)
    return snapshot


def _seed_source(connection: sqlite3.Connection, repo, commit_sha: str) -> None:
    now = utc_now()
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


def _run_index(tmp_path: Path, commit_sha: str, backend: FakeEmbeddingBackend) -> tuple[dict[str, object], Path]:
    initialize()
    repo_url = "https://github.com/mnh-shop/agent-brain.git"
    run_id = create_run(repo_url, "main", "test")
    repo = parse_repository_url(repo_url)
    snapshot = _fixture_snapshot(tmp_path)
    data_dir = Path(get_config()["storage"]["data_dir"])
    with sqlite3.connect(data_dir / "pipeline.sqlite") as connection:
        _seed_source(connection, repo, commit_sha)
    update_run(run_id, status="running", source_id=repo.source_id, commit_sha=commit_sha, resolved_branch="main", snapshot_path=str(snapshot))
    run = get_run(run_id)
    assert run is not None
    syntax.run(run)
    report = vector.run(run, backend=backend)
    update_run(run_id, status="completed", current_stage="complete", completed_at=utc_now(), error=None)
    return report, snapshot


def test_vector_stage_indexes_canonical_units_and_reruns_idempotently(tmp_path):
    _prepare_config(tmp_path)
    backend = FakeEmbeddingBackend()

    first_report, snapshot = _run_index(tmp_path / "first", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", backend)
    second_report, _ = _run_index(tmp_path / "second", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", backend)

    assert first_report["passed"] is True
    assert first_report["eligible_unit_count"] > 0
    assert first_report["indexed_vector_count"] == first_report["eligible_unit_count"]
    assert first_report["dimension_mismatch_count"] == 0
    assert first_report["metadata_filter_ok"] is True
    assert first_report["smoke_result_count"] > 0
    assert first_report["symbol_mapped_vector_count"] > 0

    assert second_report["indexed_vector_count"] == first_report["indexed_vector_count"]
    assert second_report["duplicate_unit_id_count"] > 0

    report_path = snapshot.parent / "vector-report.json"
    assert report_path.exists()


def test_vector_search_defaults_to_latest_completed_commit_and_supports_commit_filter(tmp_path, monkeypatch):
    _prepare_config(tmp_path)
    backend = FakeEmbeddingBackend()
    monkeypatch.setattr("pipeline.embeddings.LocalDeterministicEmbeddingBackend", FakeEmbeddingBackend)

    first_report, _ = _run_index(tmp_path / "first", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", backend)
    second_report, _ = _run_index(tmp_path / "second", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", backend)

    assert second_report["eligible_unit_count"] == first_report["eligible_unit_count"] * 2

    default_results = vector_search("helper", source_id="github-mnh-shop-agent-brain", limit=5)
    assert default_results
    assert all(row["commit_sha"] == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" for row in default_results)

    filtered_results = vector_search(
        "helper",
        source_id="github-mnh-shop-agent-brain",
        commit_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        limit=5,
    )
    assert filtered_results
    assert all(row["commit_sha"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" for row in filtered_results)
