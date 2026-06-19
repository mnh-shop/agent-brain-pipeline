from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml

from pipeline.config import get_config
from pipeline.db import create_run, initialize, update_run
from pipeline.stages._structure import ToolProbe, normalize_graph
from pipeline.stages import structure, syntax
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


def test_normalized_structure_fixture_maps_symbols_and_edges(tmp_path):
    root = Path(__file__).parents[1]
    fixture = root / "tests" / "fixtures" / "structure"
    raw_dir = tmp_path / "codegraph" / "raw"
    normalized_dir = tmp_path / "codegraph" / "normalized"
    raw_dir.mkdir(parents=True)
    normalized_dir.mkdir(parents=True)
    shutil.copy2(fixture / "nodes.jsonl", raw_dir / "nodes.jsonl")
    shutil.copy2(fixture / "edges.jsonl", raw_dir / "edges.jsonl")
    shutil.copy2(fixture / "queries.jsonl", raw_dir / "queries.jsonl")

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    syntax_dir = snapshot.parent / "syntax"
    syntax_dir.mkdir(parents=True)
    files_dir = snapshot.parent / "normalization"
    files_dir.mkdir(parents=True)
    (syntax_dir / "symbols.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "symbol_id": "raw-node-1",
                        "source_id": "source",
                        "platform": "github",
                        "repository_url": "https://github.com/example/repo.git",
                        "namespace": "example",
                        "repository_name": "repo",
                        "commit_sha": "deadbeef",
                        "path": "src/helper.py",
                        "normalized_path": "src/helper.py",
                        "language": "python",
                        "symbol_kind": "function",
                        "qualified_name": "example.repo.Helper",
                        "start_line": 1,
                        "end_line": 4,
                        "start_byte": 0,
                        "end_byte": 40,
                        "file_sha256": "a" * 64,
                        "content_sha256": "b" * 64,
                        "content": "def helper():\n    return 1",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "symbol_id": "raw-node-2",
                        "source_id": "source",
                        "platform": "github",
                        "repository_url": "https://github.com/example/repo.git",
                        "namespace": "example",
                        "repository_name": "repo",
                        "commit_sha": "deadbeef",
                        "path": "src/missing.py",
                        "normalized_path": "src/missing.py",
                        "language": "python",
                        "symbol_kind": "function",
                        "qualified_name": "example.repo.Missing",
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": 1,
                        "file_sha256": "c" * 64,
                        "content_sha256": "d" * 64,
                        "content": "",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (files_dir / "files.jsonl").write_text(
        json.dumps(
            {
                "path": "src/helper.py",
                "normalized_path": "src/helper.py",
                "source_id": "source",
                "commit_sha": "deadbeef",
                "file_sha256": "a" * 64,
                "content_sha256": "b" * 64,
                "source_line_start": 1,
                "source_line_end": 4,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    syntax_symbols = [
        json.loads(line)
        for line in (syntax_dir / "symbols.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    syntax_files = [
        json.loads(line)
        for line in (files_dir / "files.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    report = normalize_graph(
        run={"source_id": "source", "commit_sha": "deadbeef", "pipeline_version": "0.1.0"},
        snapshot=snapshot,
        raw_dir=raw_dir,
        normalized_dir=normalized_dir,
        probe=ToolProbe("codegraphcontext", "0.5.1", "hash", "", ["find", "bundle", "index"]),
        command_results={"list": {"command": ["codegraphcontext", "list"], "returncode": 0, "stdout_path": "raw/list.stdout.txt", "stderr_path": "raw/list.stderr.txt"}},
        syntax_symbols=syntax_symbols,
        syntax_files=syntax_files,
    )

    nodes = (normalized_dir / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
    edges = (normalized_dir / "edges.jsonl").read_text(encoding="utf-8").splitlines()
    matches = (normalized_dir / "symbol-matches.jsonl").read_text(encoding="utf-8").splitlines()
    unmatched = (normalized_dir / "unmatched-nodes.jsonl").read_text(encoding="utf-8").splitlines()
    assert nodes
    assert edges
    assert matches
    assert unmatched
    assert report["node_count"] >= 2
    assert report["edge_count"] >= 1


@pytest.mark.integration
def test_structure_stage_integration_with_real_tool(tmp_path):
    if shutil.which("codegraphcontext") is None:
        pytest.skip("CodeGraphContext binary is not installed")
    _prepare_config(tmp_path)
    initialize()
    repo_url = "https://github.com/mnh-shop/agent-brain.git"
    run_id = create_run(repo_url, "main", "test")
    repo = parse_repository_url(repo_url)
    snapshot = tmp_path / "snapshot"
    shutil.copytree(Path(__file__).parents[1] / "tests" / "fixtures" / "syntax_repo", snapshot)
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
            (repo.source_id, repo.platform, repo.normalized, repo.namespace, repo.name, repo.name, "main", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", now, now, now, now),
        )
    update_run(run_id, source_id=repo.source_id, commit_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", resolved_branch="main", snapshot_path=str(snapshot))
    run = {"run_id": run_id, "source_id": repo.source_id, "repository_url": repo.normalized, "snapshot_path": str(snapshot), "commit_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "pipeline_version": "0.1.0"}
    syntax.run(run)
    report = structure.run(run)
    assert report["tool"] == "CodeGraphContext"
    assert Path(report["bundle_path"]).exists()
