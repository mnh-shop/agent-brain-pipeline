from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pipeline.config import data_dir
from pipeline.util import utc_now

_DB_LOCK = threading.RLock()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    repository_url TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name TEXT NOT NULL,
    default_branch TEXT,
    latest_commit TEXT,
    last_ingested_at TEXT,
    next_refresh_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
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
    completed_at TEXT,
    FOREIGN KEY(source_id) REFERENCES sources(source_id)
);

CREATE TABLE IF NOT EXISTS stage_results (
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    owner_profile TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    report_path TEXT,
    error TEXT,
    PRIMARY KEY(run_id, stage),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS files (
    run_id TEXT NOT NULL,
    path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    mime_type TEXT,
    encoding TEXT,
    is_binary INTEGER NOT NULL,
    duplicate_of TEXT,
    PRIMARY KEY(run_id, path),
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS units (
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
    metadata_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
    unit_id UNINDEXED,
    source_id UNINDEXED,
    path,
    heading,
    content,
    tokenize='unicode61'
);

CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source_id, created_at);
CREATE INDEX IF NOT EXISTS idx_units_source ON units(source_id, commit_sha, path);
"""


def db_path() -> Path:
    return data_dir() / "pipeline.sqlite"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    with _DB_LOCK:
        connection = sqlite3.connect(db_path(), timeout=60, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


def initialize() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)


def create_run(repository_url: str, requested_ref: str | None, trigger: str) -> str:
    run_id = f"INGEST-{uuid.uuid4().hex[:12].upper()}"
    now = utc_now()
    with connect() as connection:
        connection.execute(
            "INSERT INTO runs(run_id, repository_url, requested_ref, trigger, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, repository_url, requested_ref, trigger, "queued", now, now),
        )
    return run_id


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        stages = connection.execute("SELECT * FROM stage_results WHERE run_id=? ORDER BY started_at", (run_id,)).fetchall()
        result["stages"] = [dict(stage) for stage in stages]
        return result


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]


def claim_next_run() -> dict[str, Any] | None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        now = utc_now()
        connection.execute("UPDATE runs SET status='running', updated_at=? WHERE run_id=?", (now, row["run_id"]))
        value = dict(row)
        value["status"] = "running"
        return value


def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    columns = ", ".join(f"{name}=?" for name in fields)
    values = list(fields.values()) + [run_id]
    with connect() as connection:
        connection.execute(f"UPDATE runs SET {columns} WHERE run_id=?", values)


def set_stage(run_id: str, stage: str, owner_profile: str, status: str, **fields: Any) -> None:
    now = utc_now()
    started = fields.pop("started_at", now if status == "running" else None)
    completed = fields.pop("completed_at", now if status in {"passed", "failed", "skipped"} else None)
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO stage_results(run_id, stage, owner_profile, status, started_at, completed_at, report_path, error)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, stage) DO UPDATE SET
              owner_profile=excluded.owner_profile,
              status=excluded.status,
              started_at=COALESCE(stage_results.started_at, excluded.started_at),
              completed_at=excluded.completed_at,
              report_path=excluded.report_path,
              error=excluded.error
            """,
            (run_id, stage, owner_profile, status, started, completed, fields.get("report_path"), fields.get("error")),
        )


def replace_files(run_id: str, rows: list[dict[str, Any]]) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM files WHERE run_id=?", (run_id,))
        connection.executemany(
            "INSERT INTO files(run_id,path,size_bytes,sha256,mime_type,encoding,is_binary,duplicate_of) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    run_id,
                    row["path"],
                    row["size_bytes"],
                    row["sha256"],
                    row.get("mime_type"),
                    row.get("encoding"),
                    1 if row.get("is_binary") else 0,
                    row.get("duplicate_of"),
                )
                for row in rows
            ],
        )


def replace_units(run_id: str, units: list[dict[str, Any]]) -> None:
    with connect() as connection:
        old_ids = [r[0] for r in connection.execute("SELECT unit_id FROM units WHERE run_id=?", (run_id,)).fetchall()]
        if old_ids:
            connection.executemany("DELETE FROM units_fts WHERE unit_id=?", [(x,) for x in old_ids])
        connection.execute("DELETE FROM units WHERE run_id=?", (run_id,))
        connection.executemany(
            """
            INSERT INTO units(unit_id,run_id,source_id,commit_sha,path,unit_type,heading,start_line,end_line,language,content_hash,content,metadata_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    row["unit_id"], run_id, row["source_id"], row["commit_sha"], row["path"], row["unit_type"],
                    row.get("heading"), row.get("start_line"), row.get("end_line"), row.get("language"),
                    row["content_hash"], row["content"], json.dumps(row.get("metadata", {}), ensure_ascii=False),
                )
                for row in units
            ],
        )
        connection.executemany(
            "INSERT INTO units_fts(unit_id,source_id,path,heading,content) VALUES(?,?,?,?,?)",
            [(row["unit_id"], row["source_id"], row["path"], row.get("heading") or "", row["content"]) for row in units],
        )
