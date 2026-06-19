from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # pragma: no cover - exercised in Docker/build environments
    import lancedb  # type: ignore
except Exception:  # pragma: no cover - local test fallback
    lancedb = None


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _vector_json(vector: Sequence[float]) -> str:
    return json.dumps([float(value) for value in vector], ensure_ascii=False)


def _vector_from_json(value: str) -> list[float]:
    parsed = json.loads(value)
    return [float(item) for item in parsed]


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _escape_filter_value(value: Any) -> str:
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _filter_expression(filters: dict[str, Any] | None) -> str | None:
    if not filters:
        return None
    clauses = []
    for key, value in filters.items():
        if value is None:
            continue
        clauses.append(f"{key} = {_escape_filter_value(value)}")
    return " AND ".join(clauses) if clauses else None


def _matches_filters(row: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    if not filters:
        return True
    for key, value in filters.items():
        if value is None:
            continue
        if str(row.get(key)) != str(value):
            return False
    return True


@dataclass
class LanceDBIndex:
    path: Path
    table_name: str = "units"
    metric: str = "cosine"

    def __post_init__(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)

    @property
    def backend(self) -> str:
        return "lancedb" if lancedb is not None else "sqlite-fallback"

    @property
    def sqlite_path(self) -> Path:
        return self.path / f"{self.table_name}.sqlite"

    def rebuild(self, rows: Sequence[dict[str, Any]]) -> None:
        records = [dict(row) for row in rows]
        if lancedb is not None:
            db = lancedb.connect(str(self.path))
            if self.table_name in getattr(db, "table_names", lambda: [])():
                db.drop_table(self.table_name)
            db.create_table(self.table_name, data=records, mode="overwrite")
            return

        with sqlite3.connect(self.sqlite_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    unit_id TEXT PRIMARY KEY,
                    source_id TEXT,
                    platform TEXT,
                    repository_url TEXT,
                    commit_sha TEXT,
                    path TEXT,
                    normalized_path TEXT,
                    unit_type TEXT,
                    symbol_id TEXT,
                    heading TEXT,
                    language TEXT,
                    start_line INTEGER,
                    end_line INTEGER,
                    start_byte INTEGER,
                    end_byte INTEGER,
                    content TEXT,
                    content_sha256 TEXT,
                    embedding_model TEXT,
                    model_revision TEXT,
                    vector_dimensions INTEGER,
                    vector_checksum TEXT,
                    created_pipeline_version TEXT,
                    run_id TEXT,
                    metadata_json TEXT NOT NULL,
                    vector_json TEXT NOT NULL
                )
                """
            )
            connection.execute("DELETE FROM vectors")
            connection.executemany(
                """
                INSERT OR REPLACE INTO vectors(
                    unit_id,source_id,platform,repository_url,commit_sha,path,normalized_path,unit_type,symbol_id,
                    heading,language,start_line,end_line,start_byte,end_byte,content,content_sha256,embedding_model,
                    model_revision,vector_dimensions,vector_checksum,created_pipeline_version,run_id,metadata_json,vector_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        row.get("unit_id"),
                        row.get("source_id"),
                        row.get("platform"),
                        row.get("repository_url"),
                        row.get("commit_sha"),
                        row.get("path"),
                        row.get("normalized_path"),
                        row.get("unit_type"),
                        row.get("symbol_id"),
                        row.get("heading"),
                        row.get("language"),
                        row.get("start_line"),
                        row.get("end_line"),
                        row.get("start_byte"),
                        row.get("end_byte"),
                        row.get("content"),
                        row.get("content_sha256"),
                        row.get("embedding_model"),
                        row.get("model_revision"),
                        row.get("vector_dimensions"),
                        row.get("vector_checksum"),
                        row.get("created_pipeline_version"),
                        row.get("run_id"),
                        json.dumps(row.get("metadata", {}), ensure_ascii=False, default=_json_default),
                        _vector_json(row.get("vector", [])),
                    )
                    for row in records
                ],
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_vectors_source ON vectors(source_id, commit_sha, path)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_vectors_unit ON vectors(unit_id)")

    def count(self, filters: dict[str, Any] | None = None) -> int:
        if lancedb is not None:
            db = lancedb.connect(str(self.path))
            table = db.open_table(self.table_name)
            if filters:
                return len(self.search([1.0], filters=filters))
            if hasattr(table, "count_rows"):
                return int(table.count_rows())
            if hasattr(table, "to_arrow"):
                arrow = table.to_arrow()
                return int(getattr(arrow, "num_rows", len(arrow)))
            raise RuntimeError("Unable to count LanceDB rows with the installed client")

        with sqlite3.connect(self.sqlite_path) as connection:
            query = "SELECT count(*) FROM vectors"
            params: list[Any] = []
            if filters:
                clauses = []
                for key, value in filters.items():
                    if value is None:
                        continue
                    clauses.append(f"{key}=?")
                    params.append(str(value))
                if clauses:
                    query += " WHERE " + " AND ".join(clauses)
            return int(connection.execute(query, params).fetchone()[0])

    def rows(self, filters: dict[str, Any] | None = None, *, dimensions: int | None = None) -> list[dict[str, Any]]:
        if lancedb is not None:
            db = lancedb.connect(str(self.path))
            table = db.open_table(self.table_name)
            expr = _filter_expression(filters)
            dimension = max(1, int(dimensions or 1))
            data = table.search([0.0] * dimension).limit(1000000)
            if expr:
                data = data.where(expr)
            return [dict(row) for row in data.to_list()]

        with sqlite3.connect(self.sqlite_path) as connection:
            connection.row_factory = sqlite3.Row
            query = "SELECT * FROM vectors"
            params: list[Any] = []
            if filters:
                clauses = []
                for key, value in filters.items():
                    if value is None:
                        continue
                    clauses.append(f"{key}=?")
                    params.append(str(value))
                if clauses:
                    query += " WHERE " + " AND ".join(clauses)
            rows = connection.execute(query, params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                item["vector"] = _vector_from_json(item.pop("vector_json") or "[]")
                result.append(item)
            return result

    def search(self, vector: Sequence[float], limit: int = 10, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if lancedb is not None:
            db = lancedb.connect(str(self.path))
            table = db.open_table(self.table_name)
            expr = _filter_expression(filters)
            query = table.search(list(vector))
            if expr:
                query = query.where(expr)
            results = query.limit(limit).to_list()
            normalized = []
            for row in results:
                item = dict(row)
                distance = item.pop("_distance", None)
                item["score"] = 1.0 - float(distance) if distance is not None else item.get("score", 0.0)
                item["distance"] = float(distance) if distance is not None else None
                normalized.append(item)
            return normalized

        rows = [row for row in self.rows(filters) if _matches_filters(row, filters)]
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            score = _cosine_similarity(vector, row.get("vector", []))
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        results = []
        for score, row in scored[:limit]:
            item = dict(row)
            item["score"] = score
            item["distance"] = 1.0 - score
            results.append(item)
        return results
