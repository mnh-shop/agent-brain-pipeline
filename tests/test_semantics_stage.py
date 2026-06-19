from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from pipeline.config import get_config
from pipeline.db import create_run, initialize, update_run
from pipeline.stages import semantics
from pipeline.stages._semantics import ToolProbe, normalize_semantic_outputs, parse_projects_output, _project_from_index
from pipeline.urls import parse_repository_url


def _prepare_config(tmp_path: Path) -> Path:
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["storage"]["data_dir"] = str(tmp_path / "data")
    cfg["storage"]["obsidian_dir"] = str(tmp_path / "vault")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    os.environ["AGENT_BRAIN_CONFIG"] = str(config_path)
    get_config.cache_clear()
    return config_path


def _seed_snapshot(tmp_path: Path) -> Path:
    root = Path(__file__).parents[1]
    snapshot = tmp_path / "snapshot"
    shutil.copytree(root / "tests" / "fixtures" / "syntax_repo", snapshot)
    return snapshot


def test_project_from_index_and_project_list_parsing():
    root = Path(__file__).parents[1]
    index = json.loads((root / "tests" / "fixtures" / "codebase_memory" / "index.json").read_text())
    projects = parse_projects_output((root / "tests" / "fixtures" / "codebase_memory" / "list_projects.json").read_text())
    assert _project_from_index(index, "fallback") == "repo-123"
    assert projects[0]["project"] == "repo-123"


def test_normalize_semantic_fixtures_maps_to_canonical_records(tmp_path):
    root = Path(__file__).parents[1]
    snapshot = _seed_snapshot(tmp_path)
    syntax_dir = snapshot.parent / "syntax"
    normalization_dir = snapshot.parent / "normalization"
    syntax_dir.mkdir(parents=True)
    normalization_dir.mkdir(parents=True)

    syntax_symbols = [
        {
            "symbol_id": "symbol-helper",
            "source_id": "source",
            "platform": "github",
            "repository_url": "https://github.com/example/repo.git",
            "namespace": "example",
            "repository_name": "repo",
            "commit_sha": "deadbeef",
            "path": "src/app.py",
            "normalized_path": "src/app.py",
            "language": "python",
            "symbol_kind": "function",
            "qualified_name": "pkg.helper",
            "start_line": 1,
            "end_line": 4,
            "start_byte": 0,
            "end_byte": 42,
            "file_sha256": "a" * 64,
            "content_sha256": "3b5d5c3712955042212316173ccf37be800e3c00000000000000000000000000",
            "content": "def helper():\n    return 1",
        }
    ]
    syntax_units = [
        {
            "unit_id": "unit-helper",
            "source_id": "source",
            "platform": "github",
            "repository_url": "https://github.com/example/repo.git",
            "namespace": "example",
            "repository_name": "repo",
            "commit_sha": "deadbeef",
            "path": "src/app.py",
            "normalized_path": "src/app.py",
            "unit_type": "function",
            "start_line": 1,
            "end_line": 4,
            "content_sha256": "3b5d5c3712955042212316173ccf37be800e3c00000000000000000000000000",
            "content": "def helper():\n    return 1",
        }
    ]
    (syntax_dir / "symbols.jsonl").write_text("".join(json.dumps(row) + "\n" for row in syntax_symbols), encoding="utf-8")
    (normalization_dir / "units.jsonl").write_text("".join(json.dumps(row) + "\n" for row in syntax_units), encoding="utf-8")

    result = normalize_semantic_outputs(
        run={"source_id": "source", "commit_sha": "deadbeef", "pipeline_version": "0.1.0"},
        snapshot=snapshot,
        raw_dir=tmp_path / "raw",
        normalized_dir=tmp_path / "normalized",
        index_output=json.loads((root / "tests" / "fixtures" / "codebase_memory" / "index.json").read_text()),
        projects_output=json.loads((root / "tests" / "fixtures" / "codebase_memory" / "list_projects.json").read_text()),
        architecture_output=json.loads((root / "tests" / "fixtures" / "codebase_memory" / "architecture.json").read_text()),
        graph_output=json.loads((root / "tests" / "fixtures" / "codebase_memory" / "graph.json").read_text()),
        semantic_output=json.loads((root / "tests" / "fixtures" / "codebase_memory" / "semantic.json").read_text()),
        probe=ToolProbe("codebase-memory-mcp", "0.8.1", "hash", "", ["cli", "list_projects", "get_architecture", "search_graph", "semantic_query"]),
        command_results={"index": {"index_mode": "full", "workers": 4, "cache_dir": str(tmp_path / "cache")}},
    )

    manifest = result["manifest"]
    assert manifest["project"] == "repo-123"
    assert manifest["project_in_list_projects"] is True
    assert manifest["graph_artifact_exists"] is False
    assert manifest["matched_count"] >= 1
    assert manifest["all_exact_commit"] is True
    assert result["semantic_records"]
    assert result["graph_records"]
    assert result["symbol_records"]
    assert result["relationship_records"]


@pytest.mark.integration
def test_semantics_stage_integration_skips_without_binary(tmp_path):
    if shutil.which("codebase-memory-mcp") is None:
        pytest.skip("Codebase-Memory binary is not installed")
    _prepare_config(tmp_path)
    initialize()
    repo_url = "https://github.com/mnh-shop/agent-brain.git"
    run_id = create_run(repo_url, "main", "test")
    repo = parse_repository_url(repo_url)
    snapshot = _seed_snapshot(tmp_path / "work")
    with __import__("sqlite3").connect(Path(tmp_path / "data" / "pipeline.sqlite")) as connection:
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
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
            (repo.source_id, repo.platform, repo.normalized, repo.namespace, repo.name, repo.name, "main", "deadbeef", now, now, now, now),
        )
    update_run(run_id, source_id=repo.source_id, commit_sha="deadbeef", resolved_branch="main", snapshot_path=str(snapshot))
    report = semantics.run({"run_id": run_id, "source_id": repo.source_id, "repository_url": repo.normalized, "snapshot_path": str(snapshot), "commit_sha": "deadbeef"})
    assert Path(snapshot.parent / "codebase-memory-report.json").exists()
    assert report["tool"] == "codebase-memory-mcp"
