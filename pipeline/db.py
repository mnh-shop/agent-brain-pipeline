from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pipeline.config import data_dir
from pipeline.schemas.ids import snapshot_identity, stable_hash, symbol_identity, unit_identity
from pipeline.util import utc_now

PIPELINE_VERSION = "0.1.0"
SCHEMA_VERSION = 3
_DB_LOCK = threading.RLock()


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


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _create_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def _create_base_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
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
            run_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            path TEXT NOT NULL,
            unit_type TEXT NOT NULL,
            heading TEXT,
            start_line INTEGER,
            end_line INTEGER,
            start_byte INTEGER,
            end_byte INTEGER,
            language TEXT,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            file_sha256 TEXT,
            qualified_name TEXT,
            parser_name TEXT,
            parser_version TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            pipeline_version TEXT NOT NULL DEFAULT '0.1.0',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(run_id, unit_id),
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
            run_id UNINDEXED,
            unit_id UNINDEXED,
            source_id UNINDEXED,
            path,
            heading,
            content,
            tokenize='unicode61'
        );

        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            repository_url TEXT NOT NULL,
            namespace TEXT NOT NULL,
            repository_name TEXT NOT NULL,
            requested_ref TEXT,
            resolved_branch TEXT,
            commit_sha TEXT NOT NULL,
            snapshot_path TEXT NOT NULL,
            raw_path TEXT,
            source_manifest_path TEXT,
            created_by_run_id TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            pipeline_version TEXT NOT NULL DEFAULT '0.1.0',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS symbols (
            run_id TEXT NOT NULL,
            symbol_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            path TEXT NOT NULL,
            language TEXT NOT NULL,
            symbol_kind TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            start_byte INTEGER,
            end_byte INTEGER,
            start_line INTEGER,
            end_line INTEGER,
            content_sha256 TEXT NOT NULL,
            content TEXT NOT NULL,
            parser_name TEXT,
            parser_version TEXT,
            unit_id TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            pipeline_version TEXT NOT NULL DEFAULT '0.1.0',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(run_id, symbol_id),
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tool_executions (
            execution_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_version TEXT,
            command_json TEXT NOT NULL,
            returncode INTEGER NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            stdout_tail TEXT,
            stderr_tail TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            schema_version INTEGER NOT NULL DEFAULT 1,
            pipeline_version TEXT NOT NULL DEFAULT '0.1.0',
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS stage_reports (
            report_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            source_id TEXT,
            commit_sha TEXT,
            report_path TEXT,
            payload_json TEXT NOT NULL,
            passed INTEGER NOT NULL,
            summary TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            pipeline_version TEXT NOT NULL DEFAULT '0.1.0',
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_sources_repo ON sources(platform, repository_url);
        CREATE INDEX IF NOT EXISTS idx_sources_commit ON sources(source_id, latest_commit);
        CREATE INDEX IF NOT EXISTS idx_files_source_commit_path ON files(run_id, path);
        CREATE INDEX IF NOT EXISTS idx_units_source_commit_path ON units(source_id, commit_sha, path);
        CREATE INDEX IF NOT EXISTS idx_units_run_path ON units(run_id, path);
        CREATE INDEX IF NOT EXISTS idx_units_unit_id ON units(unit_id);
        CREATE INDEX IF NOT EXISTS idx_symbols_source_commit_path ON symbols(source_id, commit_sha, path, qualified_name);
        CREATE INDEX IF NOT EXISTS idx_symbols_run_symbol ON symbols(run_id, symbol_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_commit ON snapshots(source_id, commit_sha);
        CREATE INDEX IF NOT EXISTS idx_tool_executions_run_stage ON tool_executions(run_id, stage);
        CREATE INDEX IF NOT EXISTS idx_stage_reports_run_stage ON stage_reports(run_id, stage);
        """
    )


def _add_columns(connection: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    existing = _table_columns(connection, table)
    for name, ddl in columns:
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _rebuild_units_table(connection: sqlite3.Connection) -> None:
    columns = _table_columns(connection, "units")
    if "run_id" in columns and "unit_id" in columns:
        info = connection.execute("PRAGMA index_list(units)").fetchall()
        composite_pk = any(row[2] for row in info) and any(name == "sqlite_autoindex_units_1" for _, name, unique, *_ in info)
        if composite_pk:
            return

    connection.execute("ALTER TABLE units RENAME TO units_legacy")
    connection.executescript(
        """
        CREATE TABLE units (
            run_id TEXT NOT NULL,
            unit_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            path TEXT NOT NULL,
            unit_type TEXT NOT NULL,
            heading TEXT,
            start_line INTEGER,
            end_line INTEGER,
            start_byte INTEGER,
            end_byte INTEGER,
            language TEXT,
            content_hash TEXT NOT NULL,
            content TEXT NOT NULL,
            file_sha256 TEXT,
            qualified_name TEXT,
            parser_name TEXT,
            parser_version TEXT,
            schema_version INTEGER NOT NULL DEFAULT 1,
            pipeline_version TEXT NOT NULL DEFAULT '0.1.0',
            provenance_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(run_id, unit_id),
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE VIRTUAL TABLE units_fts USING fts5(
            run_id UNINDEXED,
            unit_id UNINDEXED,
            source_id UNINDEXED,
            path,
            heading,
            content,
            tokenize='unicode61'
        );
        """
    )
    legacy_columns = _table_columns(connection, "units_legacy")
    copy_columns = [c for c in (
        "run_id", "unit_id", "source_id", "commit_sha", "path", "unit_type", "heading",
        "start_line", "end_line", "start_byte", "end_byte", "language", "content_hash",
        "content", "file_sha256", "qualified_name", "parser_name", "parser_version",
        "schema_version", "pipeline_version", "provenance_json",
    ) if c in legacy_columns]
    rows = connection.execute(f"SELECT {', '.join(copy_columns)} FROM units_legacy").fetchall()
    if rows:
        connection.executemany(
            f"INSERT INTO units({', '.join(copy_columns)}) VALUES({', '.join('?' for _ in copy_columns)})",
            [tuple(row[col] for col in copy_columns) for row in rows],
        )
        connection.executemany(
            "INSERT INTO units_fts(run_id, unit_id, source_id, path, heading, content) VALUES(?,?,?,?,?,?)",
            [
                (
                    row["run_id"],
                    row["unit_id"],
                    row["source_id"],
                    row["path"],
                    row["heading"] or "",
                    row["content"],
                )
                for row in rows
            ],
        )
    connection.execute("DROP TABLE units_legacy")


def _migrate_1(connection: sqlite3.Connection) -> None:
    _create_base_schema(connection)


def _migrate_2(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "sources",
        [
            ("repository_name", "repository_name TEXT"),
            ("requested_ref", "requested_ref TEXT"),
            ("resolved_branch", "resolved_branch TEXT"),
            ("source_type", "source_type TEXT NOT NULL DEFAULT 'repository'"),
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
        ],
    )
    _add_columns(
        connection,
        "files",
        [
            ("file_id", "file_id TEXT"),
            ("source_id", "source_id TEXT"),
            ("commit_sha", "commit_sha TEXT"),
            ("content_hash", "content_hash TEXT"),
            ("source_line_start", "source_line_start INTEGER"),
            ("source_line_end", "source_line_end INTEGER"),
            ("source_byte_start", "source_byte_start INTEGER"),
            ("source_byte_end", "source_byte_end INTEGER"),
            ("parser_name", "parser_name TEXT"),
            ("parser_version", "parser_version TEXT"),
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
            ("provenance_json", "provenance_json TEXT NOT NULL DEFAULT '{}'"),
        ],
    )
    _add_columns(
        connection,
        "units",
        [
            ("start_byte", "start_byte INTEGER"),
            ("end_byte", "end_byte INTEGER"),
            ("file_sha256", "file_sha256 TEXT"),
            ("qualified_name", "qualified_name TEXT"),
            ("parser_name", "parser_name TEXT"),
            ("parser_version", "parser_version TEXT"),
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
            ("provenance_json", "provenance_json TEXT NOT NULL DEFAULT '{}'"),
        ],
    )
    _add_columns(
        connection,
        "symbols",
        [
            ("run_id", "run_id TEXT"),
            ("symbol_id", "symbol_id TEXT"),
            ("source_id", "source_id TEXT"),
            ("commit_sha", "commit_sha TEXT"),
            ("path", "path TEXT"),
            ("language", "language TEXT"),
            ("symbol_kind", "symbol_kind TEXT"),
            ("qualified_name", "qualified_name TEXT"),
            ("start_byte", "start_byte INTEGER"),
            ("end_byte", "end_byte INTEGER"),
            ("start_line", "start_line INTEGER"),
            ("end_line", "end_line INTEGER"),
            ("content_sha256", "content_sha256 TEXT"),
            ("content", "content TEXT"),
            ("parser_name", "parser_name TEXT"),
            ("parser_version", "parser_version TEXT"),
            ("unit_id", "unit_id TEXT"),
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
            ("provenance_json", "provenance_json TEXT NOT NULL DEFAULT '{}'"),
        ],
    )
    _add_columns(
        connection,
        "snapshots",
        [
            ("snapshot_id", "snapshot_id TEXT"),
            ("source_id", "source_id TEXT"),
            ("platform", "platform TEXT"),
            ("repository_url", "repository_url TEXT"),
            ("namespace", "namespace TEXT"),
            ("repository_name", "repository_name TEXT"),
            ("requested_ref", "requested_ref TEXT"),
            ("resolved_branch", "resolved_branch TEXT"),
            ("commit_sha", "commit_sha TEXT"),
            ("snapshot_path", "snapshot_path TEXT"),
            ("raw_path", "raw_path TEXT"),
            ("source_manifest_path", "source_manifest_path TEXT"),
            ("created_by_run_id", "created_by_run_id TEXT"),
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
            ("created_at", "created_at TEXT NOT NULL DEFAULT ''"),
        ],
    )
    _add_columns(
        connection,
        "tool_executions",
        [
            ("execution_id", "execution_id TEXT"),
            ("run_id", "run_id TEXT"),
            ("stage", "stage TEXT"),
            ("tool_name", "tool_name TEXT"),
            ("tool_version", "tool_version TEXT"),
            ("command_json", "command_json TEXT NOT NULL DEFAULT '[]'"),
            ("returncode", "returncode INTEGER NOT NULL DEFAULT 0"),
            ("started_at", "started_at TEXT"),
            ("completed_at", "completed_at TEXT"),
            ("stdout_tail", "stdout_tail TEXT"),
            ("stderr_tail", "stderr_tail TEXT"),
            ("metadata_json", "metadata_json TEXT NOT NULL DEFAULT '{}'"),
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
        ],
    )
    _add_columns(
        connection,
        "stage_reports",
        [
            ("report_id", "report_id TEXT"),
            ("run_id", "run_id TEXT"),
            ("stage", "stage TEXT"),
            ("source_id", "source_id TEXT"),
            ("commit_sha", "commit_sha TEXT"),
            ("report_path", "report_path TEXT"),
            ("payload_json", "payload_json TEXT NOT NULL DEFAULT '{}'"),
            ("passed", "passed INTEGER NOT NULL DEFAULT 0"),
            ("summary", "summary TEXT"),
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
            ("created_at", "created_at TEXT NOT NULL DEFAULT ''"),
        ],
    )
    _add_columns(
        connection,
        "runs",
        [
            ("schema_version", "schema_version INTEGER NOT NULL DEFAULT 1"),
            ("pipeline_version", "pipeline_version TEXT NOT NULL DEFAULT '0.1.0'"),
        ],
    )
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_symbols_source_commit_path ON symbols(source_id, commit_sha, path, qualified_name);
        CREATE INDEX IF NOT EXISTS idx_symbols_run_symbol ON symbols(run_id, symbol_id);
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_commit ON snapshots(source_id, commit_sha);
        CREATE INDEX IF NOT EXISTS idx_tool_executions_run_stage ON tool_executions(run_id, stage);
        CREATE INDEX IF NOT EXISTS idx_stage_reports_run_stage ON stage_reports(run_id, stage);
        """
    )


def _migrate_3(connection: sqlite3.Connection) -> None:
    _rebuild_units_table(connection)
    connection.execute("DROP INDEX IF EXISTS idx_units_source")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_units_source_commit_path ON units(source_id, commit_sha, path)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_units_run_path ON units(run_id, path)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_units_unit_id ON units(unit_id)")


MIGRATIONS: dict[int, Any] = {
    1: _migrate_1,
    2: _migrate_2,
    3: _migrate_3,
}


def initialize() -> None:
    with connect() as connection:
        _create_migration_table(connection)
        current = connection.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        for version in range(int(current) + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if not migration:
                continue
            migration(connection)
            connection.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (version, utc_now()),
            )


def create_run(repository_url: str, requested_ref: str | None, trigger: str) -> str:
    run_id = f"INGEST-{uuid.uuid4().hex[:12].upper()}"
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO runs(run_id, repository_url, requested_ref, trigger, status, created_at, updated_at, schema_version, pipeline_version)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (run_id, repository_url, requested_ref, trigger, "queued", now, now, 1, PIPELINE_VERSION),
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
        result["reports"] = {stage["stage"]: stage["report_path"] for stage in stages if stage["report_path"]}
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


def upsert_snapshot(row: dict[str, Any]) -> str:
    snapshot_id = row.get("snapshot_id") or snapshot_identity(
        source_id=row["source_id"],
        commit_sha=row["commit_sha"],
        snapshot_path=row["snapshot_path"],
    )
    payload = {
        "snapshot_id": snapshot_id,
        "source_id": row["source_id"],
        "platform": row["platform"],
        "repository_url": row["repository_url"],
        "namespace": row["namespace"],
        "repository_name": row["repository_name"],
        "requested_ref": row.get("requested_ref"),
        "resolved_branch": row.get("resolved_branch"),
        "commit_sha": row["commit_sha"],
        "snapshot_path": row["snapshot_path"],
        "raw_path": row.get("raw_path"),
        "source_manifest_path": row.get("source_manifest_path"),
        "created_by_run_id": row.get("created_by_run_id"),
        "schema_version": row.get("schema_version", 1),
        "pipeline_version": row.get("pipeline_version", PIPELINE_VERSION),
        "created_at": row.get("created_at", utc_now()),
    }
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO snapshots(
                snapshot_id, source_id, platform, repository_url, namespace, repository_name,
                requested_ref, resolved_branch, commit_sha, snapshot_path, raw_path,
                source_manifest_path, created_by_run_id, schema_version, pipeline_version, created_at
            )
            VALUES(:snapshot_id,:source_id,:platform,:repository_url,:namespace,:repository_name,:requested_ref,:resolved_branch,:commit_sha,:snapshot_path,:raw_path,:source_manifest_path,:created_by_run_id,:schema_version,:pipeline_version,:created_at)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                requested_ref=excluded.requested_ref,
                resolved_branch=excluded.resolved_branch,
                snapshot_path=excluded.snapshot_path,
                raw_path=excluded.raw_path,
                source_manifest_path=excluded.source_manifest_path,
                created_by_run_id=excluded.created_by_run_id,
                pipeline_version=excluded.pipeline_version
            """,
            payload,
        )
    return snapshot_id


def replace_files(run_id: str, rows: list[dict[str, Any]]) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM files WHERE run_id=?", (run_id,))
        connection.executemany(
            """
            INSERT INTO files(
                run_id, path, size_bytes, sha256, mime_type, encoding, is_binary, duplicate_of,
                file_id, source_id, commit_sha, content_hash, source_line_start, source_line_end,
                source_byte_start, source_byte_end, parser_name, parser_version, schema_version,
                pipeline_version, provenance_json
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
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
                    row.get("file_id") or stable_hash([run_id, row["path"], row["sha256"]]),
                    row.get("source_id"),
                    row.get("commit_sha"),
                    row.get("content_hash") or row["sha256"],
                    row.get("source_line_start"),
                    row.get("source_line_end"),
                    row.get("source_byte_start"),
                    row.get("source_byte_end"),
                    row.get("parser_name"),
                    row.get("parser_version"),
                    row.get("schema_version", 1),
                    row.get("pipeline_version", PIPELINE_VERSION),
                    json.dumps(row.get("provenance", {}), ensure_ascii=False),
                )
                for row in rows
            ],
        )


def replace_units(run_id: str, units: list[dict[str, Any]], symbols: list[dict[str, Any]] | None = None) -> None:
    symbols = symbols or []
    with connect() as connection:
        old_ids = [r[0] for r in connection.execute("SELECT unit_id FROM units WHERE run_id=?", (run_id,)).fetchall()]
        if old_ids:
            connection.executemany("DELETE FROM units_fts WHERE run_id=? AND unit_id=?", [(run_id, x) for x in old_ids])
        connection.execute("DELETE FROM units WHERE run_id=?", (run_id,))
        connection.execute("DELETE FROM symbols WHERE run_id=?", (run_id,))
        connection.executemany(
            """
            INSERT INTO units(
                run_id, unit_id, source_id, commit_sha, path, unit_type, heading,
                start_line, end_line, start_byte, end_byte, language, content_hash, content,
                file_sha256, qualified_name, parser_name, parser_version, schema_version,
                pipeline_version, provenance_json
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    run_id,
                    row["unit_id"],
                    row["source_id"],
                    row["commit_sha"],
                    row["path"],
                    row["unit_type"],
                    row.get("heading"),
                    row.get("start_line"),
                    row.get("end_line"),
                    row.get("start_byte"),
                    row.get("end_byte"),
                    row.get("language"),
                    row["content_hash"],
                    row["content"],
                    row.get("file_sha256"),
                    row.get("qualified_name"),
                    row.get("parser_name"),
                    row.get("parser_version"),
                    row.get("schema_version", 1),
                    row.get("pipeline_version", PIPELINE_VERSION),
                    json.dumps(row.get("provenance", {}), ensure_ascii=False),
                )
                for row in units
            ],
        )
        connection.executemany(
            "INSERT INTO units_fts(run_id, unit_id, source_id, path, heading, content) VALUES(?,?,?,?,?,?)",
            [(run_id, row["unit_id"], row["source_id"], row["path"], row.get("heading") or "", row["content"]) for row in units],
        )
        if symbols:
            connection.executemany(
                """
                INSERT INTO symbols(
                    run_id, symbol_id, source_id, commit_sha, path, language, symbol_kind,
                    qualified_name, start_byte, end_byte, start_line, end_line, content_sha256,
                    content, parser_name, parser_version, unit_id, schema_version, pipeline_version,
                    provenance_json
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        run_id,
                        row["symbol_id"],
                        row["source_id"],
                        row["commit_sha"],
                        row["path"],
                        row["language"],
                        row["symbol_kind"],
                        row["qualified_name"],
                        row.get("start_byte"),
                        row.get("end_byte"),
                        row.get("start_line"),
                        row.get("end_line"),
                        row["content_hash"],
                        row["content"],
                        row.get("parser_name"),
                        row.get("parser_version"),
                        row.get("unit_id"),
                        row.get("schema_version", 1),
                        row.get("pipeline_version", PIPELINE_VERSION),
                        json.dumps(row.get("provenance", {}), ensure_ascii=False),
                    )
                    for row in symbols
                ],
            )


def record_tool_execution(row: dict[str, Any]) -> None:
    payload = {
        "execution_id": row["execution_id"],
        "run_id": row["run_id"],
        "stage": row["stage"],
        "tool_name": row["tool_name"],
        "tool_version": row.get("tool_version"),
        "command_json": json.dumps(row.get("command", []), ensure_ascii=False),
        "returncode": row["returncode"],
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "stdout_tail": row.get("stdout_tail"),
        "stderr_tail": row.get("stderr_tail"),
        "metadata_json": json.dumps(row.get("metadata", {}), ensure_ascii=False),
        "schema_version": row.get("schema_version", 1),
        "pipeline_version": row.get("pipeline_version", PIPELINE_VERSION),
    }
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO tool_executions(
                execution_id, run_id, stage, tool_name, tool_version, command_json, returncode,
                started_at, completed_at, stdout_tail, stderr_tail, metadata_json, schema_version, pipeline_version
            )
            VALUES(:execution_id,:run_id,:stage,:tool_name,:tool_version,:command_json,:returncode,:started_at,:completed_at,:stdout_tail,:stderr_tail,:metadata_json,:schema_version,:pipeline_version)
            ON CONFLICT(execution_id) DO UPDATE SET
                completed_at=excluded.completed_at,
                stdout_tail=excluded.stdout_tail,
                stderr_tail=excluded.stderr_tail,
                metadata_json=excluded.metadata_json,
                returncode=excluded.returncode
            """,
            payload,
        )


def record_stage_report(row: dict[str, Any]) -> None:
    payload = {
        "report_id": row["report_id"],
        "run_id": row["run_id"],
        "stage": row["stage"],
        "source_id": row.get("source_id"),
        "commit_sha": row.get("commit_sha"),
        "report_path": row.get("report_path"),
        "payload_json": json.dumps(row["payload"], ensure_ascii=False),
        "passed": 1 if row.get("passed") else 0,
        "summary": row.get("summary"),
        "schema_version": row.get("schema_version", 1),
        "pipeline_version": row.get("pipeline_version", PIPELINE_VERSION),
        "created_at": row.get("created_at", utc_now()),
    }
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO stage_reports(
                report_id, run_id, stage, source_id, commit_sha, report_path, payload_json,
                passed, summary, schema_version, pipeline_version, created_at
            )
            VALUES(:report_id,:run_id,:stage,:source_id,:commit_sha,:report_path,:payload_json,:passed,:summary,:schema_version,:pipeline_version,:created_at)
            ON CONFLICT(report_id) DO UPDATE SET
                report_path=excluded.report_path,
                payload_json=excluded.payload_json,
                passed=excluded.passed,
                summary=excluded.summary
            """,
            payload,
        )


def make_unit_id(*, source_id: str, commit_sha: str, path: str, unit_type: str, start: int | None, end: int | None, content_hash: str) -> str:
    return unit_identity(source_id=source_id, commit_sha=commit_sha, path=path, unit_type=unit_type, start=start, end=end, content_hash=content_hash)


def make_symbol_id(
    *,
    source_id: str,
    commit_sha: str,
    path: str,
    language: str,
    symbol_kind: str,
    qualified_name: str,
    start_byte: int | None,
    end_byte: int | None,
    content_hash: str,
) -> str:
    return symbol_identity(
        source_id=source_id,
        commit_sha=commit_sha,
        path=path,
        language=language,
        symbol_kind=symbol_kind,
        qualified_name=qualified_name,
        start_byte=start_byte,
        end_byte=end_byte,
        content_hash=content_hash,
    )
