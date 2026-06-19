from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from pipeline.config import get_config
from pipeline.db import connect, create_run, get_run, initialize, update_run
from pipeline.stages import lint, normalize


def _write_config(tmp_path: Path) -> Path:
    config = {
        "storage": {
            "data_dir": str(tmp_path / "data"),
            "obsidian_dir": str(tmp_path / "vault"),
        },
        "scm": {"github_token": "", "gitlab_token": ""},
        "pipeline": {
            "max_text_file_bytes": 1024 * 1024,
            "code_chunk_lines": 40,
            "code_chunk_overlap_lines": 5,
            "markdown_chunk_characters": 200,
            "ignored_directories": [],
            "ignored_globs": [],
        },
        "lint": {
            "enabled": True,
            "fail_severities": ["error", "fatal"],
            "check_external_links": False,
        },
    }
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _reset_config(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("AGENT_BRAIN_CONFIG", str(path))
    get_config.cache_clear()


def test_normalize_and_lint_cover_expected_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = _write_config(tmp_path)
    _reset_config(monkeypatch, cfg)
    initialize()

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "good.txt").write_text("hello world\n", encoding="utf-8")
    (snapshot / "dup1.txt").write_text("same content\n", encoding="utf-8")
    (snapshot / "dup2.txt").write_text("same content\n", encoding="utf-8")
    (snapshot / "bad.json").write_text('{"a": }', encoding="utf-8")
    (snapshot / "bad.yaml").write_text("a: [", encoding="utf-8")
    (snapshot / "bad.toml").write_text("a = ", encoding="utf-8")
    (snapshot / "bad.py").write_text("def x(:\n    pass\n", encoding="utf-8")
    (snapshot / "broken.md").write_text("# Title\n\n[broken](missing.md)\n", encoding="utf-8")
    (snapshot / "duplicate.md").write_text("# Same\n\n## Same\n", encoding="utf-8")
    (snapshot / "frontmatter.md").write_text("---\na: [\n---\n# Heading\n", encoding="utf-8")
    (snapshot / "latin1.txt").write_bytes("café\n".encode("latin-1"))

    run_id = create_run("https://github.com/example/repo.git", "main", "manual")
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO sources(source_id, platform, repository_url, namespace, name, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                "github-example-repo",
                "github",
                "https://github.com/example/repo.git",
                "example",
                "repo",
                "2026-06-19T00:00:00+00:00",
                "2026-06-19T00:00:00+00:00",
            ),
        )
    update_run(
        run_id,
        source_id="github-example-repo",
        commit_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        snapshot_path=str(snapshot),
    )
    run = get_run(run_id)
    assert run is not None
    run["repository_url"] = "https://github.com/example/repo.git"
    run["platform"] = "github"
    run["namespace"] = "example"
    run["repository_name"] = "repo"
    run["resolved_branch"] = "main"

    normalize_report = normalize.run(run)
    assert normalize_report["file_count"] >= 8
    assert (snapshot.parent / "files.jsonl").exists()
    assert (snapshot.parent / "units.jsonl").exists()

    (snapshot.parent / "integrity-report.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
    lint_report = lint.run(run)

    checks = {item["check"]: item for item in lint_report["findings"] if "check" in item}
    assert any(item["check"] == "json_parse" for item in lint_report["findings"])
    assert any(item["check"] == "yaml_parse" for item in lint_report["findings"])
    assert any(item["check"] == "toml_parse" for item in lint_report["findings"])
    assert any(item["check"] == "python_compile" for item in lint_report["findings"])
    assert any(item["check"] == "broken_markdown_link" for item in lint_report["findings"])
    assert any(item["check"] == "markdown_frontmatter" for item in lint_report["findings"])
    assert any(item["check"] == "duplicate_heading" for item in lint_report["findings"])
    assert any(item["check"] == "duplicate_anchor" for item in lint_report["findings"])
    assert any(item["check"] == "duplicate_content" for item in lint_report["findings"])
    assert any(item["check"] == "mixed_encoding" for item in lint_report["findings"])
    assert not lint_report["passed"]
    assert (snapshot.parent / "lint-report.json").exists()
    compat = json.loads((snapshot.parent / "curate-report.json").read_text(encoding="utf-8"))
    assert compat["integrity"]["passed"] is True
    assert compat["normalization"]["passed"] is True
    assert compat["lint"]["passed"] is False
