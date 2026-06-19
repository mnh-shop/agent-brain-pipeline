from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from pipeline.config import data_dir
from pipeline.schemas.ids import normalize_path, snapshot_identity, stable_hash, symbol_identity, unit_identity
from pipeline.util import utc_now

PIPELINE_VERSION = "0.1.0"
SCHEMA_VERSION = 5
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
    return {row["name"] for row in rows}


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_index(connection: sqlite3.Connection, name: str, sql: str) -> None:
    connection.execute(sql.replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))


def _migration_versions(connection: sqlite3.Connection) -> set[int]:
    if not connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone():
        return set()
    rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row["version"]) for row in rows}


def _record_migration(connection: sqlite3.Connection, version: int) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?,?)",
        (version, utc_now()),
    )


def _migrate_1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            repository_url TEXT NOT NULL,
            namespace TEXT NOT NULL,
            name TEXT NOT NULL,
            repository_name TEXT,
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
            resolved_branch TEXT,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            current_stage TEXT,
            commit_sha TEXT,
            snapshot_path TEXT,
            wiki_state TEXT,
            wiki_job_id TEXT,
            wiki_started_at TEXT,
            wiki_completed_at TEXT,
            wiki_failed_at TEXT,
            wiki_error TEXT,
            wiki_manifest_path TEXT,
            wiki_evidence_path TEXT,
            wiki_page_manifest_path TEXT,
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
            source_id TEXT,
            platform TEXT,
            repository_url TEXT,
            namespace TEXT,
            repository_name TEXT,
            requested_ref TEXT,
            resolved_branch TEXT,
            commit_sha TEXT,
            file_sha256 TEXT,
            content_sha256 TEXT,
            source_line_start INTEGER,
            source_line_end INTEGER,
            source_byte_start INTEGER,
            source_byte_end INTEGER,
            generator_name TEXT,
            generator_version TEXT,
            schema_version INTEGER,
            pipeline_version TEXT,
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
            path TEXT NOT NULL,
            raw_path TEXT,
            bundle_path TEXT,
            archive_path TEXT,
            mirror_archive_path TEXT,
            source_line_start INTEGER,
            source_line_end INTEGER,
            source_byte_start INTEGER,
            source_byte_end INTEGER,
            file_sha256 TEXT,
            content_sha256 TEXT,
            generator_name TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS symbols (
            symbol_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            repository_url TEXT NOT NULL,
            namespace TEXT NOT NULL,
            repository_name TEXT NOT NULL,
            requested_ref TEXT,
            resolved_branch TEXT,
            commit_sha TEXT NOT NULL,
            path TEXT NOT NULL,
            normalized_path TEXT NOT NULL,
            language TEXT NOT NULL,
            symbol_kind TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            start_byte INTEGER,
            end_byte INTEGER,
            start_line INTEGER,
            end_line INTEGER,
            file_sha256 TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            content TEXT,
            generator_name TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tool_executions (
            execution_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_version TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            command TEXT,
            input_json TEXT,
            output_json TEXT,
            error TEXT,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS stage_reports (
            report_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            source_id TEXT,
            commit_sha TEXT,
            status TEXT NOT NULL,
            passed INTEGER NOT NULL,
            summary_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            errors_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_units_source ON units(source_id, commit_sha, path);
        CREATE INDEX IF NOT EXISTS idx_units_run ON units(run_id, path);
        CREATE INDEX IF NOT EXISTS idx_symbols_source ON symbols(source_id, commit_sha, path);
        CREATE INDEX IF NOT EXISTS idx_symbols_run ON symbols(run_id, path);
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_commit ON snapshots(source_id, commit_sha);
        CREATE INDEX IF NOT EXISTS idx_tool_executions_run_stage ON tool_executions(run_id, stage);
        CREATE INDEX IF NOT EXISTS idx_stage_reports_run_stage ON stage_reports(run_id, stage);
        """
    )


def _migrate_2(connection: sqlite3.Connection) -> None:
    for column, definition in (
        ("repository_name", "TEXT"),
        ("resolved_branch", "TEXT"),
    ):
        _ensure_column(connection, "sources", column, definition)
    for column, definition in (
        ("requested_ref", "TEXT"),
        ("resolved_branch", "TEXT"),
    ):
        _ensure_column(connection, "runs", column, definition)
    for column, definition in (
        ("source_id", "TEXT"),
        ("platform", "TEXT"),
        ("repository_url", "TEXT"),
        ("namespace", "TEXT"),
        ("repository_name", "TEXT"),
        ("requested_ref", "TEXT"),
        ("resolved_branch", "TEXT"),
        ("commit_sha", "TEXT"),
        ("file_sha256", "TEXT"),
        ("content_sha256", "TEXT"),
        ("source_line_start", "INTEGER"),
        ("source_line_end", "INTEGER"),
        ("source_byte_start", "INTEGER"),
        ("source_byte_end", "INTEGER"),
        ("generator_name", "TEXT"),
        ("generator_version", "TEXT"),
        ("schema_version", "INTEGER"),
        ("pipeline_version", "TEXT"),
    ):
        _ensure_column(connection, "files", column, definition)
    for column, definition in (
        ("platform", "TEXT"),
        ("repository_url", "TEXT"),
        ("namespace", "TEXT"),
        ("repository_name", "TEXT"),
        ("requested_ref", "TEXT"),
        ("resolved_branch", "TEXT"),
        ("normalized_path", "TEXT"),
        ("start_byte", "INTEGER"),
        ("end_byte", "INTEGER"),
        ("file_sha256", "TEXT"),
        ("content_sha256", "TEXT"),
        ("generator_name", "TEXT"),
        ("generator_version", "TEXT"),
        ("schema_version", "INTEGER"),
        ("pipeline_version", "TEXT"),
        ("source_line_start", "INTEGER"),
        ("source_line_end", "INTEGER"),
        ("source_byte_start", "INTEGER"),
        ("source_byte_end", "INTEGER"),
    ):
        _ensure_column(connection, "units", column, definition)

    # Older databases may already have the canonical tables; ensure they exist.
    connection.executescript(
        """
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
            path TEXT NOT NULL,
            raw_path TEXT,
            bundle_path TEXT,
            archive_path TEXT,
            mirror_archive_path TEXT,
            source_line_start INTEGER,
            source_line_end INTEGER,
            source_byte_start INTEGER,
            source_byte_end INTEGER,
            file_sha256 TEXT,
            content_sha256 TEXT,
            generator_name TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS symbols (
            symbol_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            repository_url TEXT NOT NULL,
            namespace TEXT NOT NULL,
            repository_name TEXT NOT NULL,
            requested_ref TEXT,
            resolved_branch TEXT,
            commit_sha TEXT NOT NULL,
            path TEXT NOT NULL,
            normalized_path TEXT NOT NULL,
            language TEXT NOT NULL,
            symbol_kind TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            start_byte INTEGER,
            end_byte INTEGER,
            start_line INTEGER,
            end_line INTEGER,
            file_sha256 TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            content TEXT,
            generator_name TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_executions (
            execution_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_version TEXT,
            status TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            command TEXT,
            input_json TEXT,
            output_json TEXT,
            error TEXT,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stage_reports (
            report_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            source_id TEXT,
            commit_sha TEXT,
            status TEXT NOT NULL,
            passed INTEGER NOT NULL,
            summary_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL,
            errors_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            pipeline_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    _ensure_index(connection, "idx_runs_status", "CREATE INDEX idx_runs_status ON runs(status, created_at)")
    _ensure_index(connection, "idx_runs_source", "CREATE INDEX idx_runs_source ON runs(source_id, created_at)")
    _ensure_index(connection, "idx_units_source", "CREATE INDEX idx_units_source ON units(source_id, commit_sha, path)")
    _ensure_index(connection, "idx_units_run", "CREATE INDEX idx_units_run ON units(run_id, path)")
    _ensure_index(connection, "idx_symbols_source", "CREATE INDEX idx_symbols_source ON symbols(source_id, commit_sha, path)")
    _ensure_index(connection, "idx_symbols_run", "CREATE INDEX idx_symbols_run ON symbols(run_id, path)")
    _ensure_index(connection, "idx_snapshots_source_commit", "CREATE INDEX idx_snapshots_source_commit ON snapshots(source_id, commit_sha)")
    _ensure_index(connection, "idx_tool_executions_run_stage", "CREATE INDEX idx_tool_executions_run_stage ON tool_executions(run_id, stage)")
    _ensure_index(connection, "idx_stage_reports_run_stage", "CREATE INDEX idx_stage_reports_run_stage ON stage_reports(run_id, stage)")


def _copy_unit_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    normalized_path = data.get("normalized_path") or normalize_path(data.get("path", ""))
    content = data.get("content", "")
    content_sha256 = data.get("content_sha256") or data.get("content_hash") or stable_hash(content)
    file_sha256 = data.get("file_sha256") or data.get("sha256") or content_sha256
    return {
        "unit_id": data["unit_id"],
        "run_id": data["run_id"],
        "source_id": data["source_id"],
        "platform": data.get("platform") or "unknown",
        "repository_url": data.get("repository_url") or "",
        "namespace": data.get("namespace") or "",
        "repository_name": (
            data.get("repository_name")
            or data.get("name")
            or (Path(data.get("path", "")).parts[0] if data.get("path") else "repository")
        ),
        "requested_ref": data.get("requested_ref"),
        "resolved_branch": data.get("resolved_branch"),
        "commit_sha": data["commit_sha"],
        "path": data["path"],
        "normalized_path": normalized_path,
        "unit_type": data.get("unit_type", "section"),
        "heading": data.get("heading"),
        "language": data.get("language"),
        "start_line": data.get("start_line"),
        "end_line": data.get("end_line"),
        "start_byte": data.get("start_byte"),
        "end_byte": data.get("end_byte"),
        "file_sha256": file_sha256 or content_sha256,
        "content_sha256": content_sha256,
        "content": content,
        "generator_name": data.get("generator_name") or "curate",
        "generator_version": data.get("generator_version") or "1",
        "schema_version": int(data.get("schema_version") or 1),
        "pipeline_version": data.get("pipeline_version") or PIPELINE_VERSION,
        "source_line_start": data.get("source_line_start") or data.get("start_line"),
        "source_line_end": data.get("source_line_end") or data.get("end_line"),
        "source_byte_start": data.get("source_byte_start") or data.get("start_byte"),
        "source_byte_end": data.get("source_byte_end") or data.get("end_byte"),
        "metadata_json": data.get("metadata_json") or json.dumps(data.get("metadata") or {}, ensure_ascii=False),
    }


def _migrate_3(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        old_rows = connection.execute("SELECT * FROM units").fetchall() if _table_columns(connection, "units") else []
        connection.execute("DROP TABLE IF EXISTS units_new")
        connection.execute(
            """
            CREATE TABLE units_new (
                run_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                repository_url TEXT NOT NULL,
                namespace TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                requested_ref TEXT,
                resolved_branch TEXT,
                commit_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                normalized_path TEXT NOT NULL,
                unit_type TEXT NOT NULL,
                heading TEXT,
                language TEXT,
                start_line INTEGER,
                end_line INTEGER,
                start_byte INTEGER,
                end_byte INTEGER,
                file_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                content TEXT NOT NULL,
                generator_name TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                pipeline_version TEXT NOT NULL,
                source_line_start INTEGER,
                source_line_end INTEGER,
                source_byte_start INTEGER,
                source_byte_end INTEGER,
                metadata_json TEXT NOT NULL,
                PRIMARY KEY(run_id, unit_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute("DELETE FROM units_new")
        for row in old_rows:
            copied = _copy_unit_row(row)
            connection.execute(
                """
                INSERT OR REPLACE INTO units_new(
                    run_id, unit_id, source_id, platform, repository_url, namespace, repository_name,
                    requested_ref, resolved_branch, commit_sha, path, normalized_path, unit_type, heading,
                    language, start_line, end_line, start_byte, end_byte, file_sha256, content_sha256,
                    content, generator_name, generator_version, schema_version, pipeline_version,
                    source_line_start, source_line_end, source_byte_start, source_byte_end, metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    copied["run_id"],
                    copied["unit_id"],
                    copied["source_id"],
                    copied["platform"],
                    copied["repository_url"],
                    copied["namespace"],
                    copied["repository_name"],
                    copied["requested_ref"],
                    copied["resolved_branch"],
                    copied["commit_sha"],
                    copied["path"],
                    copied["normalized_path"],
                    copied["unit_type"],
                    copied["heading"],
                    copied["language"],
                    copied["start_line"],
                    copied["end_line"],
                    copied["start_byte"],
                    copied["end_byte"],
                    copied["file_sha256"],
                    copied["content_sha256"],
                    copied["content"],
                    copied["generator_name"],
                    copied["generator_version"],
                    copied["schema_version"],
                    copied["pipeline_version"],
                    copied["source_line_start"],
                    copied["source_line_end"],
                    copied["source_byte_start"],
                    copied["source_byte_end"],
                    copied["metadata_json"],
                ),
            )

        connection.execute("DROP TABLE IF EXISTS units")
        connection.execute("ALTER TABLE units_new RENAME TO units")
        connection.execute("DROP TABLE IF EXISTS units_fts")
        connection.execute(
            """
            CREATE VIRTUAL TABLE units_fts USING fts5(
                run_id UNINDEXED,
                unit_id UNINDEXED,
                source_id UNINDEXED,
                path,
                heading,
                content,
                tokenize='unicode61'
            )
            """
        )
        connection.executemany(
            "INSERT INTO units_fts(run_id,unit_id,source_id,path,heading,content) VALUES(?,?,?,?,?,?)",
            [
                (row["run_id"], row["unit_id"], row["source_id"], row["path"], row["heading"] or "", row["content"])
                for row in connection.execute("SELECT run_id, unit_id, source_id, path, heading, content FROM units").fetchall()
            ],
        )
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def _migrate_4(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        symbol_rows = connection.execute("SELECT * FROM symbols").fetchall() if _table_columns(connection, "symbols") else []
        connection.execute("DROP TABLE IF EXISTS symbols_new")
        connection.execute(
            """
            CREATE TABLE symbols_new (
                run_id TEXT NOT NULL,
                symbol_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                repository_url TEXT NOT NULL,
                namespace TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                requested_ref TEXT,
                resolved_branch TEXT,
                commit_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                normalized_path TEXT NOT NULL,
                language TEXT NOT NULL,
                symbol_kind TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                start_byte INTEGER,
                end_byte INTEGER,
                start_line INTEGER,
                end_line INTEGER,
                file_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                content TEXT,
                generator_name TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                pipeline_version TEXT NOT NULL,
                PRIMARY KEY(run_id, symbol_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            )
            """
        )
        for row in symbol_rows:
            connection.execute(
                """
                INSERT OR REPLACE INTO symbols_new(
                    run_id,symbol_id,source_id,platform,repository_url,namespace,repository_name,
                    requested_ref,resolved_branch,commit_sha,path,normalized_path,language,symbol_kind,
                    qualified_name,start_byte,end_byte,start_line,end_line,file_sha256,content_sha256,content,
                    generator_name,generator_version,schema_version,pipeline_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row["run_id"],
                    row["symbol_id"],
                    row["source_id"],
                    row["platform"],
                    row["repository_url"],
                    row["namespace"],
                    row["repository_name"],
                    row["requested_ref"],
                    row["resolved_branch"],
                    row["commit_sha"],
                    row["path"],
                    row["normalized_path"],
                    row["language"],
                    row["symbol_kind"],
                    row["qualified_name"],
                    row["start_byte"],
                    row["end_byte"],
                    row["start_line"],
                    row["end_line"],
                    row["file_sha256"],
                    row["content_sha256"],
                    row["content"],
                    row["generator_name"],
                    row["generator_version"],
                    row["schema_version"],
                    row["pipeline_version"],
                ),
            )
        connection.execute("DROP TABLE IF EXISTS symbols")
        connection.execute("ALTER TABLE symbols_new RENAME TO symbols")

        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS imports (
                run_id TEXT NOT NULL,
                import_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                repository_url TEXT NOT NULL,
                namespace TEXT NOT NULL,
                repository_name TEXT NOT NULL,
                requested_ref TEXT,
                resolved_branch TEXT,
                commit_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                normalized_path TEXT NOT NULL,
                language TEXT NOT NULL,
                import_kind TEXT NOT NULL,
                imported_name TEXT NOT NULL,
                imported_as TEXT,
                start_byte INTEGER,
                end_byte INTEGER,
                start_line INTEGER,
                end_line INTEGER,
                file_sha256 TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                content TEXT,
                generator_name TEXT NOT NULL,
                generator_version TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                pipeline_version TEXT NOT NULL,
                PRIMARY KEY(run_id, import_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_imports_source ON imports(source_id, commit_sha, path);
            CREATE INDEX IF NOT EXISTS idx_imports_run ON imports(run_id, path);
            """
        )
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def _migrate_5(connection: sqlite3.Connection) -> None:
    for column, definition in (
        ("wiki_state", "TEXT"),
        ("wiki_job_id", "TEXT"),
        ("wiki_started_at", "TEXT"),
        ("wiki_completed_at", "TEXT"),
        ("wiki_failed_at", "TEXT"),
        ("wiki_error", "TEXT"),
        ("wiki_manifest_path", "TEXT"),
        ("wiki_evidence_path", "TEXT"),
        ("wiki_page_manifest_path", "TEXT"),
    ):
        _ensure_column(connection, "runs", column, definition)
    _ensure_index(connection, "idx_runs_wiki_state", "CREATE INDEX idx_runs_wiki_state ON runs(wiki_state, updated_at)")


def initialize() -> None:
    with connect() as connection:
        connection.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;"
        )
        existing_versions = _migration_versions(connection)
        if 1 not in existing_versions:
            _migrate_1(connection)
            _record_migration(connection, 1)
        if 2 not in _migration_versions(connection):
            _migrate_2(connection)
            _record_migration(connection, 2)
        if 3 not in _migration_versions(connection):
            _migrate_3(connection)
            _record_migration(connection, 3)
        if 4 not in _migration_versions(connection):
            _migrate_4(connection)
            _record_migration(connection, 4)
        if 5 not in _migration_versions(connection):
            _migrate_5(connection)
            _record_migration(connection, 5)


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


def upsert_snapshot(record: Mapping[str, Any]) -> None:
    now = utc_now()
    payload = dict(record)
    payload.setdefault("created_at", now)
    payload["updated_at"] = now
    columns = [
        "snapshot_id",
        "source_id",
        "platform",
        "repository_url",
        "namespace",
        "repository_name",
        "requested_ref",
        "resolved_branch",
        "commit_sha",
        "path",
        "raw_path",
        "bundle_path",
        "archive_path",
        "mirror_archive_path",
        "source_line_start",
        "source_line_end",
        "source_byte_start",
        "source_byte_end",
        "file_sha256",
        "content_sha256",
        "generator_name",
        "generator_version",
        "schema_version",
        "pipeline_version",
        "created_at",
        "updated_at",
    ]
    with connect() as connection:
        connection.execute(
            f"""
            INSERT INTO snapshots({','.join(columns)})
            VALUES({','.join('?' for _ in columns)})
            ON CONFLICT(snapshot_id) DO UPDATE SET
              source_id=excluded.source_id,
              platform=excluded.platform,
              repository_url=excluded.repository_url,
              namespace=excluded.namespace,
              repository_name=excluded.repository_name,
              requested_ref=excluded.requested_ref,
              resolved_branch=excluded.resolved_branch,
              commit_sha=excluded.commit_sha,
              path=excluded.path,
              raw_path=excluded.raw_path,
              bundle_path=excluded.bundle_path,
              archive_path=excluded.archive_path,
              mirror_archive_path=excluded.mirror_archive_path,
              source_line_start=excluded.source_line_start,
              source_line_end=excluded.source_line_end,
              source_byte_start=excluded.source_byte_start,
              source_byte_end=excluded.source_byte_end,
              file_sha256=excluded.file_sha256,
              content_sha256=excluded.content_sha256,
              generator_name=excluded.generator_name,
              generator_version=excluded.generator_version,
              schema_version=excluded.schema_version,
              pipeline_version=excluded.pipeline_version,
              updated_at=excluded.updated_at
            """,
            [payload.get(column) for column in columns],
        )


def replace_files(run_id: str, rows: list[dict[str, Any]]) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM files WHERE run_id=?", (run_id,))
        connection.executemany(
            """
            INSERT INTO files(
                run_id,path,size_bytes,sha256,mime_type,encoding,is_binary,duplicate_of,
                source_id,platform,repository_url,namespace,repository_name,requested_ref,resolved_branch,
                commit_sha,file_sha256,content_sha256,source_line_start,source_line_end,
                source_byte_start,source_byte_end,generator_name,generator_version,schema_version,pipeline_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    row.get("source_id"),
                    row.get("platform"),
                    row.get("repository_url"),
                    row.get("namespace"),
                    row.get("repository_name"),
                    row.get("requested_ref"),
                    row.get("resolved_branch"),
                    row.get("commit_sha"),
                    row.get("file_sha256") or row["sha256"],
                    row.get("content_sha256"),
                    row.get("source_line_start"),
                    row.get("source_line_end"),
                    row.get("source_byte_start"),
                    row.get("source_byte_end"),
                    row.get("generator_name") or "curate",
                    row.get("generator_version") or "1",
                    row.get("schema_version") or 1,
                    row.get("pipeline_version") or PIPELINE_VERSION,
                )
                for row in rows
            ],
        )


def replace_units(
    run_id: str,
    units: list[dict[str, Any]],
    symbols: list[dict[str, Any]] | None = None,
    imports: list[dict[str, Any]] | None = None,
) -> None:
    with connect() as connection:
        old_ids = [r[0] for r in connection.execute("SELECT unit_id FROM units WHERE run_id=?", (run_id,)).fetchall()]
        if old_ids:
            connection.executemany(
                "DELETE FROM units_fts WHERE run_id=? AND unit_id=?",
                [(run_id, x) for x in old_ids],
            )
        connection.execute("DELETE FROM units WHERE run_id=?", (run_id,))
        unit_rows: list[tuple[Any, ...]] = []
        for row in units:
            source_id = row["source_id"]
            commit_sha = row["commit_sha"]
            path = row["path"]
            unit_type = row["unit_type"]
            start_line = row.get("start_line")
            end_line = row.get("end_line")
            content = row["content"]
            content_sha256 = row.get("content_sha256") or row.get("content_hash") or stable_hash(content)
            normalized = row.get("normalized_path") or normalize_path(path)
            unit_id = row.get("unit_id") or unit_identity(source_id, commit_sha, normalized, unit_type, start_line, end_line, content_sha256)
            metadata = row.get("metadata", {})
            unit_rows.append(
                (
                    run_id,
                    unit_id,
                    source_id,
                    row.get("platform") or "",
                    row.get("repository_url") or "",
                    row.get("namespace") or "",
                    row.get("repository_name") or "",
                    row.get("requested_ref"),
                    row.get("resolved_branch"),
                    commit_sha,
                    path,
                    normalized,
                    unit_type,
                    row.get("heading"),
                    row.get("language"),
                    start_line,
                    end_line,
                    row.get("start_byte"),
                    row.get("end_byte"),
                    row.get("file_sha256") or row.get("source_file_sha256") or content_sha256,
                    content_sha256,
                    content,
                    row.get("generator_name") or "curate",
                    row.get("generator_version") or "1",
                    row.get("schema_version") or 1,
                    row.get("pipeline_version") or PIPELINE_VERSION,
                    row.get("source_line_start", start_line),
                    row.get("source_line_end", end_line),
                    row.get("source_byte_start", row.get("start_byte")),
                    row.get("source_byte_end", row.get("end_byte")),
                    json.dumps(metadata, ensure_ascii=False),
                )
            )
        connection.executemany(
            """
            INSERT INTO units(
                run_id,unit_id,source_id,platform,repository_url,namespace,repository_name,
                requested_ref,resolved_branch,commit_sha,path,normalized_path,unit_type,heading,language,
                start_line,end_line,start_byte,end_byte,file_sha256,content_sha256,content,generator_name,
                generator_version,schema_version,pipeline_version,source_line_start,source_line_end,
                source_byte_start,source_byte_end,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            unit_rows,
        )
        connection.executemany(
            """
            INSERT INTO units_fts(run_id,unit_id,source_id,path,heading,content)
            VALUES(?,?,?,?,?,?)
            """,
            [
                (row[0], row[1], row[2], row[10], row[13] or "", row[21])
                for row in unit_rows
            ],
        )

        if symbols:
            connection.execute("DELETE FROM symbols WHERE run_id=?", (run_id,))
            connection.executemany(
                """
                INSERT INTO symbols(
                    symbol_id,run_id,source_id,platform,repository_url,namespace,repository_name,
                    requested_ref,resolved_branch,commit_sha,path,normalized_path,language,symbol_kind,
                    qualified_name,start_byte,end_byte,start_line,end_line,file_sha256,content_sha256,content,
                    generator_name,generator_version,schema_version,pipeline_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        row["symbol_id"],
                        run_id,
                        row["source_id"],
                        row.get("platform") or "",
                        row.get("repository_url") or "",
                        row.get("namespace") or "",
                        row.get("repository_name") or "",
                        row.get("requested_ref"),
                        row.get("resolved_branch"),
                        row["commit_sha"],
                        row["path"],
                        row.get("normalized_path") or normalize_path(row["path"]),
                        row.get("language") or "",
                        row["symbol_kind"],
                        row["qualified_name"],
                        row.get("start_byte"),
                        row.get("end_byte"),
                        row.get("start_line"),
                        row.get("end_line"),
                        row.get("file_sha256") or row.get("content_sha256") or "",
                        row.get("content_sha256") or row.get("file_sha256") or "",
                        row.get("content"),
                        row.get("generator_name") or "structure",
                        row.get("generator_version") or "1",
                        row.get("schema_version") or 1,
                        row.get("pipeline_version") or PIPELINE_VERSION,
                    )
                    for row in symbols
                ],
            )
        if imports is not None:
            connection.execute("DELETE FROM imports WHERE run_id=?", (run_id,))
            connection.executemany(
                """
                INSERT INTO imports(
                    import_id,run_id,source_id,platform,repository_url,namespace,repository_name,
                    requested_ref,resolved_branch,commit_sha,path,normalized_path,language,import_kind,
                    imported_name,imported_as,start_byte,end_byte,start_line,end_line,file_sha256,
                    content_sha256,content,generator_name,generator_version,schema_version,pipeline_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        row["import_id"],
                        run_id,
                        row["source_id"],
                        row.get("platform") or "",
                        row.get("repository_url") or "",
                        row.get("namespace") or "",
                        row.get("repository_name") or "",
                        row.get("requested_ref"),
                        row.get("resolved_branch"),
                        row["commit_sha"],
                        row["path"],
                        row.get("normalized_path") or normalize_path(row["path"]),
                        row.get("language") or "",
                        row["import_kind"],
                        row["imported_name"],
                        row.get("imported_as"),
                        row.get("start_byte"),
                        row.get("end_byte"),
                        row.get("start_line"),
                        row.get("end_line"),
                        row.get("file_sha256") or row.get("content_sha256") or "",
                        row.get("content_sha256") or row.get("file_sha256") or "",
                        row.get("content"),
                        row.get("generator_name") or "syntax",
                        row.get("generator_version") or "1",
                        row.get("schema_version") or 1,
                        row.get("pipeline_version") or PIPELINE_VERSION,
                    )
                    for row in imports
                ],
            )


def make_unit_id(
    source_id: str,
    commit_sha: str,
    path: str,
    unit_type: str,
    start: int | None,
    end: int | None,
    content_hash: str,
) -> str:
    return unit_identity(source_id, commit_sha, path, unit_type, start, end, content_hash)


def make_symbol_id(
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
    return symbol_identity(source_id, commit_sha, path, language, symbol_kind, qualified_name, start_byte, end_byte, content_hash)


def record_tool_execution(record: Mapping[str, Any]) -> None:
    payload = dict(record)
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO tool_executions(
                execution_id,run_id,stage,tool_name,tool_version,status,started_at,completed_at,command,input_json,output_json,error,
                schema_version,pipeline_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(execution_id) DO UPDATE SET
              status=excluded.status,
              started_at=excluded.started_at,
              completed_at=excluded.completed_at,
              command=excluded.command,
              input_json=excluded.input_json,
              output_json=excluded.output_json,
              error=excluded.error,
              schema_version=excluded.schema_version,
              pipeline_version=excluded.pipeline_version
            """,
            (
                payload.get("execution_id") or uuid.uuid4().hex,
                payload["run_id"],
                payload["stage"],
                payload["tool_name"],
                payload.get("tool_version"),
                payload["status"],
                payload.get("started_at"),
                payload.get("completed_at"),
                payload.get("command"),
                payload.get("input_json"),
                payload.get("output_json"),
                payload.get("error"),
                payload.get("schema_version") or 1,
                payload.get("pipeline_version") or PIPELINE_VERSION,
            ),
        )


def record_stage_report(record: Mapping[str, Any]) -> None:
    payload = dict(record)
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO stage_reports(
                report_id,run_id,stage,source_id,commit_sha,status,passed,summary_json,metrics_json,warnings_json,errors_json,
                schema_version,pipeline_version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(report_id) DO UPDATE SET
              status=excluded.status,
              passed=excluded.passed,
              summary_json=excluded.summary_json,
              metrics_json=excluded.metrics_json,
              warnings_json=excluded.warnings_json,
              errors_json=excluded.errors_json,
              schema_version=excluded.schema_version,
              pipeline_version=excluded.pipeline_version,
              updated_at=excluded.updated_at
            """,
            (
                payload.get("report_id") or uuid.uuid4().hex,
                payload["run_id"],
                payload["stage"],
                payload.get("source_id"),
                payload.get("commit_sha"),
                payload["status"],
                1 if payload.get("passed") else 0,
                json.dumps(payload.get("summary", {}), ensure_ascii=False),
                json.dumps(payload.get("metrics", {}), ensure_ascii=False),
                json.dumps(payload.get("warnings", []), ensure_ascii=False),
                json.dumps(payload.get("errors", []), ensure_ascii=False),
                payload.get("schema_version") or 1,
                payload.get("pipeline_version") or PIPELINE_VERSION,
                payload.get("created_at") or now,
                now,
            ),
        )
