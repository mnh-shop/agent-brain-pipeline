from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from pipeline.config import get_config
from pipeline.db import connect, create_run, get_run, initialize, update_run
from pipeline.urls import parse_repository_url
from pipeline.stages import syntax


def _prepare_config(tmp_path: Path) -> Path:
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["storage"]["data_dir"] = str(tmp_path / "data")
    cfg["storage"]["obsidian_dir"] = str(tmp_path / "vault")
    cfg["syntax"]["max_symbol_bytes"] = 1024 * 1024
    cfg["pipeline"]["max_text_file_bytes"] = 1024 * 1024
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    os.environ["AGENT_BRAIN_CONFIG"] = str(config_path)
    get_config.cache_clear()
    return config_path


def _fixture_snapshot(tmp_path: Path) -> Path:
    fixture = Path(__file__).parents[1] / "tests" / "fixtures" / "syntax_repo"
    snapshot = tmp_path / "snapshot"
    shutil.copytree(fixture, snapshot)
    return snapshot


def _run_syntax(tmp_path: Path, repository_url: str) -> dict[str, object]:
    initialize()
    run_id = create_run(repository_url, "main", "test")
    repo = parse_repository_url(repository_url)
    snapshot = _fixture_snapshot(tmp_path)
    with connect() as connection:
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
            (
                repo.source_id,
                repo.platform,
                repo.normalized,
                repo.namespace,
                repo.name,
                repo.name,
                "main",
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                now,
                now,
                now,
                now,
            ),
        )
    update_run(
        run_id,
        source_id=repo.source_id,
        commit_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        resolved_branch="main",
        snapshot_path=str(snapshot),
    )
    run = get_run(run_id)
    assert run is not None
    report = syntax.run(run)
    return {"run_id": run_id, "snapshot": snapshot, "report": report}


def _read_units(run_id: str) -> list[dict[str, object]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM units WHERE run_id=? ORDER BY path, start_line, start_byte",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _read_symbols(run_id: str) -> list[dict[str, object]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM symbols WHERE run_id=? ORDER BY path, start_line, start_byte",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _read_imports(run_id: str) -> list[dict[str, object]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM imports WHERE run_id=? ORDER BY path, start_line, start_byte",
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_syntax_extracts_symbols_and_imports_stably(tmp_path):
    _prepare_config(tmp_path)
    repository_url = "https://github.com/mnh-shop/agent-brain.git"
    first = _run_syntax(tmp_path / "first", repository_url)
    second = _run_syntax(tmp_path / "second", repository_url)

    first_symbols = _read_symbols(first["run_id"])
    second_symbols = _read_symbols(second["run_id"])
    assert first_symbols
    assert second_symbols
    assert [row["symbol_id"] for row in first_symbols] == [row["symbol_id"] for row in second_symbols]
    assert any(row["symbol_kind"] == "class" for row in first_symbols)
    assert any(row["symbol_kind"] == "function" for row in first_symbols)
    assert any(row["symbol_kind"] == "method" for row in first_symbols)
    assert any(row["symbol_kind"] == "configuration-object" for row in first_symbols)

    imports = _read_imports(first["run_id"])
    assert imports

    snapshot = first["snapshot"]
    for symbol in first_symbols:
        source = snapshot / Path(symbol["path"])
        assert source.exists()
        text = source.read_text(encoding="utf-8")
        start = int(symbol["start_line"])
        end = int(symbol["end_line"])
        lines = text.splitlines()
        content = "\n".join(lines[start - 1 : end])
        assert int(symbol["start_line"]) >= 1
        assert int(symbol["end_line"]) <= len(lines) or len(lines) == 0
        assert symbol["content_sha256"] == __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()

    units = _read_units(first["run_id"])
    assert units
    code_chunks = [row for row in units if row["unit_type"] == "code_chunk"]
    assert all("bad.py" in row["path"] for row in code_chunks)

    report = first["report"]
    assert report["parse_failure_count"] >= 1
    assert report["symbol_unit_count"] > 0

    syntax_report = snapshot.parent / "syntax" / "syntax-report.json"
    assert syntax_report.exists()
    parse_errors = snapshot.parent / "syntax" / "parse-errors.jsonl"
    assert parse_errors.read_text(encoding="utf-8").strip()


def test_symbol_content_matches_source_and_fts_has_no_duplicate_fallbacks(tmp_path):
    _prepare_config(tmp_path)
    repository_url = "https://github.com/mnh-shop/agent-brain.git"
    result = _run_syntax(tmp_path / "run", repository_url)
    run_id = result["run_id"]
    snapshot = result["snapshot"]
    units = _read_units(run_id)

    seen_paths = {row["path"] for row in units if row["unit_type"] == "code_chunk"}
    assert seen_paths == {"broken/bad.py"}

    for row in units:
        path = snapshot / Path(row["path"])
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        start = int(row["start_line"])
        end = int(row["end_line"])
        content = "\n".join(lines[start - 1 : end])
        assert row["content"] == content
        if row["unit_type"] != "code_chunk":
            assert row["unit_type"] in {"module", "class", "function", "method", "constructor", "interface", "enum", "configuration-object", "constant"}
