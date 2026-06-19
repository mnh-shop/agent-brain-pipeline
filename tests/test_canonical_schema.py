from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from pipeline.config import get_config
from pipeline.db import create_run, initialize, make_unit_id, replace_units
from pipeline.schemas.ids import normalize_path
from pipeline.schemas.unit import UnitRecord
from pipeline.stages.curate import _markdown_units
from pipeline.util import sha256_text


def _write_config(tmp_path: Path) -> Path:
    config = {
        "storage": {
            "data_dir": str(tmp_path / "data"),
            "obsidian_dir": str(tmp_path / "vault"),
        },
        "scm": {"github_token": "", "gitlab_token": ""},
        "maintenance": {},
        "pipeline": {},
    }
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _reset_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("AGENT_BRAIN_CONFIG", str(path))
    get_config.cache_clear()


def test_normalize_path_is_consistent():
    assert normalize_path("./a//b\\c/") == "a/b/c"
    assert normalize_path("a/b") == "a/b"


def test_unit_ids_are_stable_without_run_id():
    unit_a = make_unit_id("src", "deadbeef", "docs/readme.md", "markdown_section", 1, 4, sha256_text("hello"))
    unit_b = make_unit_id("src", "deadbeef", "./docs//readme.md", "markdown_section", 1, 4, sha256_text("hello"))
    unit_c = make_unit_id("src", "feedbead", "docs/readme.md", "markdown_section", 1, 4, sha256_text("hello"))
    assert unit_a == unit_b
    assert unit_a != unit_c


def test_markdown_units_reconstruct_from_source_lines():
    text = """# Title

First paragraph.

## Nested

Second paragraph.
More text.
"""
    units = _markdown_units(
        text,
        "docs/example.md",
        {
            "source_id": "source",
            "platform": "github",
            "repository_url": "https://github.com/example/repo.git",
            "namespace": "example",
            "repository_name": "repo",
            "requested_ref": "main",
            "resolved_branch": "main",
            "commit_sha": "deadbeef",
            "file_sha256": sha256_text(text),
            "pipeline_version": "0.1.0",
        },
        1000,
    )
    lines = text.splitlines()
    assert units
    for unit in units:
        start = unit["start_line"]
        end = unit["end_line"]
        assert unit["content"] == "\n".join(lines[start - 1:end])


def test_pydantic_validation_rejects_incomplete_provenance():
    with pytest.raises(ValidationError):
        UnitRecord(
            unit_id="x",
            source_id="source",
            platform="github",
            repository_url="https://github.com/example/repo.git",
            namespace="example",
            repository_name="repo",
            commit_sha="deadbeef",
            path="docs/example.md",
            normalized_path="docs/example.md",
            unit_type="markdown_section",
            file_sha256="a" * 64,
            content="hello",
            pipeline_version="0.1.0",
            generator_name="curate",
            generator_version="1",
        )


def test_same_commit_twice_keeps_same_unit_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _write_config(tmp_path)
    _reset_config(monkeypatch, cfg)
    initialize()
    run_one = create_run("https://github.com/example/repo.git", "main", "manual")
    run_two = create_run("https://github.com/example/repo.git", "main", "manual")

    unit = {
        "source_id": "github-example-repo",
        "platform": "github",
        "repository_url": "https://github.com/example/repo.git",
        "namespace": "example",
        "repository_name": "repo",
        "requested_ref": "main",
        "resolved_branch": "main",
        "commit_sha": "deadbeef",
        "path": "docs/example.md",
        "normalized_path": "docs/example.md",
        "unit_type": "markdown_section",
        "heading": "Title",
        "language": "markdown",
        "start_line": 1,
        "end_line": 3,
        "file_sha256": sha256_text("content"),
        "content_sha256": sha256_text("# Title\nbody"),
        "content": "# Title\nbody",
        "generator_name": "curate:markdown",
        "generator_version": "1",
        "schema_version": 1,
        "pipeline_version": "0.1.0",
        "metadata": {"source_id": "github-example-repo"},
    }
    replace_units(run_one, [unit])
    replace_units(run_two, [unit])

    with sqlite3.connect(tmp_path / "data" / "pipeline.sqlite") as connection:
        rows = connection.execute("SELECT run_id, unit_id FROM units ORDER BY run_id").fetchall()
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1]


def test_different_commits_change_unit_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _write_config(tmp_path)
    _reset_config(monkeypatch, cfg)
    initialize()

    base = {
        "source_id": "github-example-repo",
        "platform": "github",
        "repository_url": "https://github.com/example/repo.git",
        "namespace": "example",
        "repository_name": "repo",
        "requested_ref": "main",
        "resolved_branch": "main",
        "path": "docs/example.md",
        "normalized_path": "docs/example.md",
        "unit_type": "markdown_section",
        "heading": "Title",
        "language": "markdown",
        "start_line": 1,
        "end_line": 3,
        "file_sha256": sha256_text("content"),
        "generator_name": "curate:markdown",
        "generator_version": "1",
        "schema_version": 1,
        "pipeline_version": "0.1.0",
        "metadata": {"source_id": "github-example-repo"},
    }
    unit_a = dict(base, commit_sha="deadbeef", content="# Title\nbody", content_sha256=sha256_text("# Title\nbody"))
    unit_b = dict(base, commit_sha="feedbead", content="# Title\nbody changed", content_sha256=sha256_text("# Title\nbody changed"))
    assert make_unit_id(unit_a["source_id"], unit_a["commit_sha"], unit_a["path"], unit_a["unit_type"], unit_a["start_line"], unit_a["end_line"], unit_a["content_sha256"]) != make_unit_id(unit_b["source_id"], unit_b["commit_sha"], unit_b["path"], unit_b["unit_type"], unit_b["start_line"], unit_b["end_line"], unit_b["content_sha256"])


def test_migration_preserves_existing_runs_files_and_units(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _write_config(tmp_path)
    _reset_config(monkeypatch, cfg)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db = data_dir / "pipeline.sqlite"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                source_id TEXT,
                repository_url TEXT NOT NULL,
                requested_ref TEXT,
                trigger TEXT NOT NULL,
                status TEXT NOT NULL,
                current_stage TEXT,
                commit_sha TEXT,
                snapshot_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE files (
                run_id TEXT NOT NULL,
                path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT,
                encoding TEXT,
                is_binary INTEGER NOT NULL,
                duplicate_of TEXT,
                PRIMARY KEY(run_id, path)
            );
            CREATE TABLE units (
                unit_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                unit_type TEXT NOT NULL,
                heading TEXT,
                start_line INTEGER,
                end_line INTEGER,
                language TEXT,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            INSERT INTO runs(run_id, repository_url, trigger, status, created_at, updated_at)
            VALUES('RUN-1', 'https://github.com/example/repo.git', 'manual', 'completed', '2026-06-19T00:00:00Z', '2026-06-19T00:00:00Z');
            INSERT INTO files(run_id, path, size_bytes, sha256, is_binary)
            VALUES('RUN-1', 'docs/example.md', 12, 'abc', 0);
            INSERT INTO units(unit_id, run_id, source_id, commit_sha, path, unit_type, content_hash, content, metadata_json)
            VALUES('unit-1', 'RUN-1', 'github-example-repo', 'deadbeef', 'docs/example.md', 'markdown_section', 'hash', 'content', '{}');
            """
        )

    initialize()

    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM files").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM units").fetchone()[0] == 1
        assert connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()[-1][0] == 4
