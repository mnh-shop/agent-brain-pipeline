from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.config import get_config
from pipeline.db import connect, create_run, get_run, initialize, update_run
from pipeline import runner
from pipeline.util import utc_now


def _prepare_config(tmp_path: Path) -> Path:
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["storage"]["data_dir"] = str(tmp_path / "data")
    cfg["storage"]["obsidian_dir"] = str(tmp_path / "vault")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return config_path


def test_execute_run_failure_updates_run_and_stage_consistently(tmp_path, monkeypatch):
    config_path = _prepare_config(tmp_path)
    monkeypatch.setenv("AGENT_BRAIN_CONFIG", str(config_path))
    get_config.cache_clear()
    initialize()

    run_id = create_run("https://github.com/example/repo.git", "main", "test")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO sources(
                source_id, platform, repository_url, namespace, name, repository_name,
                default_branch, latest_commit, last_ingested_at, next_refresh_at, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "github-example-repo",
                "github",
                "https://github.com/example/repo.git",
                "example",
                "repo",
                "repo",
                "main",
                "deadbeef",
                now,
                now,
                now,
                now,
            ),
        )
    update_run(
        run_id,
        source_id="github-example-repo",
        commit_sha="deadbeef",
        snapshot_path=str(snapshot),
    )

    def fail_stage(_: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        runner,
        "get_config",
        lambda: {
            "stages": {
                "semantics": {"owner_profile": "semantic-analyst"},
            }
        },
    )
    monkeypatch.setitem(runner.STAGE_FUNCTIONS, "semantics", fail_stage)
    monkeypatch.setattr(runner.export, "refresh_kanban", lambda: None)

    runner.execute_run(run_id)

    run = get_run(run_id)
    assert run is not None
    assert run["status"] == "failed"
    assert run["current_stage"] == "semantics"
    assert run["error"] == "RuntimeError: boom"

    stage = next(item for item in run["stages"] if item["stage"] == "semantics")
    assert stage["status"] == "failed"
    assert stage["error"] == "RuntimeError: boom"
    assert stage["started_at"] is not None
    assert stage["completed_at"] is not None
