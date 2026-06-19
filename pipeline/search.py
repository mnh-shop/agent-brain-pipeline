from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.db import connect, get_run
from pipeline.util import component_dir, read_json, run_command

TERMINAL_SEARCH_STATUSES = ("completed", "deterministic_passed", "ready_for_wiki")


def _latest_completed_runs(source_id: str | None, commit_sha: str | None = None) -> list[dict[str, Any]]:
    max_sources = int(get_config().get("pipeline", {}).get("max_global_search_sources", 8))
    statuses = ",".join("?" for _ in TERMINAL_SEARCH_STATUSES)
    with connect() as connection:
        if source_id and commit_sha:
            rows = connection.execute(
                f"""
                SELECT run_id FROM runs
                WHERE source_id=? AND commit_sha=? AND status IN ({statuses})
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (source_id, commit_sha, *TERMINAL_SEARCH_STATUSES),
            ).fetchall()
        elif source_id:
            rows = connection.execute(
                f"SELECT run_id FROM runs WHERE source_id=? AND status IN ({statuses}) ORDER BY completed_at DESC LIMIT 1",
                (source_id, *TERMINAL_SEARCH_STATUSES),
            ).fetchall()
        elif commit_sha:
            rows = connection.execute(
                f"""
                SELECT run_id FROM (
                  SELECT run_id, source_id, completed_at,
                         ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY completed_at DESC) AS row_number
                  FROM runs
                  WHERE status IN ({statuses}) AND source_id IS NOT NULL AND commit_sha=?
                )
                WHERE row_number=1
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                (*TERMINAL_SEARCH_STATUSES, commit_sha, max_sources),
            ).fetchall()
        else:
            rows = connection.execute(
                f"""
                SELECT run_id FROM (
                  SELECT run_id, source_id, completed_at,
                         ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY completed_at DESC) AS row_number
                  FROM runs
                  WHERE status IN ({statuses}) AND source_id IS NOT NULL
                )
                WHERE row_number=1
                ORDER BY completed_at DESC
                LIMIT ?
                """,
                (*TERMINAL_SEARCH_STATUSES, max_sources),
            ).fetchall()
    runs: list[dict[str, Any]] = []
    for row in rows:
        result = get_run(row["run_id"])
        if result:
            runs.append(result)
    if not runs:
        raise ValueError("No deterministic ingestion matches the request")
    return runs


def _fts_expression(query: str) -> str:
    tokens = re.findall(r"[\w./:-]+", query, flags=re.UNICODE)
    if not tokens:
        raise ValueError("Search query contains no searchable terms")
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:24])


def _fts_rows(query: str, run_ids: list[str], limit: int) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    sql = f"""
        SELECT u.unit_id,u.source_id,u.commit_sha,u.path,u.unit_type,u.heading,u.start_line,u.end_line,u.language,
               bm25(units_fts) AS score,
               snippet(units_fts, 4, '[', ']', ' … ', 28) AS excerpt
        FROM units_fts JOIN units u ON u.run_id = units_fts.run_id AND u.unit_id = units_fts.unit_id
        WHERE units_fts MATCH ? AND u.run_id IN ({placeholders})
        ORDER BY score LIMIT ?
    """
    params: list[Any] = [_fts_expression(query), *run_ids, limit]
    with connect() as connection:
        rows = connection.execute(sql, params).fetchall()
        return [dict(row) | {"method": "fts"} for row in rows]


def fts_search_for_run(query: str, run_id: str, limit: int = 10) -> list[dict[str, Any]]:
    return _fts_rows(query, [run_id], limit)


def fts_search(query: str, source_id: str | None = None, limit: int = 10, commit_sha: str | None = None) -> list[dict[str, Any]]:
    return _fts_rows(query, [run["run_id"] for run in _latest_completed_runs(source_id, commit_sha)], limit)


def _exact_for_run(query: str, run: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    snapshot = Path(run["snapshot_path"])
    result = run_command(
        ["rg", "--json", "--line-number", "--fixed-strings", "--max-count", str(limit), "--", query, str(snapshot)],
        check=False,
        timeout=120,
    )
    items: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        path = Path(data["path"]["text"])
        try:
            relative = path.relative_to(snapshot).as_posix()
        except ValueError:
            relative = str(path)
        items.append({
            "method": "exact",
            "source_id": run["source_id"],
            "commit_sha": run["commit_sha"],
            "path": relative,
            "start_line": data.get("line_number"),
            "excerpt": data["lines"]["text"].rstrip(),
            "score": 0,
        })
        if len(items) >= limit:
            break
    return items


def exact_search_for_run(query: str, run: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    return _exact_for_run(query, run, limit)


def exact_search(query: str, source_id: str | None = None, limit: int = 20, commit_sha: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for run in _latest_completed_runs(source_id, commit_sha):
        remaining = max(1, limit - len(items))
        items.extend(_exact_for_run(query, run, remaining))
        if len(items) >= limit:
            break
    return items[:limit]


def structural_search(query: str, source_id: str | None = None, limit: int = 10, commit_sha: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    command = str(get_config().get("codegraph", {}).get("command", "codegraphcontext"))
    for run in _latest_completed_runs(source_id, commit_sha)[:limit]:
        derived = component_dir(run, "codegraph")
        env = {"HOME": str(derived / "home"), "XDG_CACHE_HOME": str(derived / "cache"), "XDG_CONFIG_HOME": str(derived / "config")}
        result = run_command([command, "find", "pattern", query], env=env, check=False, timeout=180)
        items.append({
            "method": "structural",
            "source_id": run["source_id"],
            "commit_sha": run["commit_sha"],
            "returncode": result.returncode,
            "output": result.stdout[-30000:],
            "error": result.stderr[-6000:] if result.returncode else None,
            "score": 0,
            "limit_requested": limit,
        })
    return items


def semantic_search(query: str, source_id: str | None = None, limit: int = 10, commit_sha: str | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cfg = get_config().get("codebase_memory", {})
    for run in _latest_completed_runs(source_id, commit_sha)[:limit]:
        derived = component_dir(run, "codebase-memory")
        env = {"CBM_CACHE_DIR": str(derived), "CBM_WORKERS": str(cfg.get("workers", 4)), "CBM_LOG_LEVEL": "error"}
        semantic_report = Path(run["snapshot_path"]).parent / "codebase-memory-report.json"
        legacy_report = Path(run["snapshot_path"]).parent / "semantic-report.json"
        project = str(cfg.get("project_name", "repository"))
        if semantic_report.exists():
            project = str(read_json(semantic_report).get("project") or project)
        elif legacy_report.exists():
            project = str(read_json(legacy_report).get("project") or project)
        payload = {"query": query, "limit": limit, "project": project}
        result = run_command([str(cfg.get("command", "codebase-memory-mcp")), "cli", "semantic_query", json.dumps(payload)], env=env, check=False, timeout=180)
        try:
            parsed: Any = json.loads(result.stdout)
        except json.JSONDecodeError:
            parsed = result.stdout[-30000:]
        items.append({
            "method": "semantic",
            "source_id": run["source_id"],
            "commit_sha": run["commit_sha"],
            "returncode": result.returncode,
            "results": parsed,
            "error": result.stderr[-6000:] if result.returncode else None,
            "score": 0,
        })
    return items


def vector_search(query: str, source_id: str | None = None, limit: int = 10, commit_sha: str | None = None) -> list[dict[str, Any]]:
    from pipeline.embeddings import LocalDeterministicEmbeddingBackend
    from pipeline.indexes import LanceDBIndex

    cfg = get_config()
    section = cfg.get("lancedb", {})
    embeddings_cfg = cfg.get("embeddings", {})
    backend = LocalDeterministicEmbeddingBackend(
        model=str(embeddings_cfg.get("model", "agent-brain-local-hash-embedding")),
        revision=str(embeddings_cfg.get("revision", "v1")),
        dimensions=int(embeddings_cfg.get("dimensions", 384)),
        normalize=bool(embeddings_cfg.get("normalize", True)),
    )
    index = LanceDBIndex(
        path=Path(section.get("path", "/data/indexes/lancedb")),
        table_name=str(section.get("table", "units")),
        metric=str(section.get("metric", "cosine")),
    )
    items: list[dict[str, Any]] = []
    for run in _latest_completed_runs(source_id, commit_sha)[:limit]:
        filters = {"source_id": run["source_id"], "commit_sha": run["commit_sha"]}
        results = index.search(backend.embed([query])[0], limit=limit, filters=filters)
        for row in results:
            items.append(dict(row) | {"method": "vector"})
    items.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
    return items[:limit]


def hybrid_search(query: str, source_id: str | None = None, limit: int = 10, commit_sha: str | None = None) -> dict[str, Any]:
    return {
        "query": query,
        "source_id": source_id,
        "fts": fts_search(query, source_id, limit, commit_sha),
        "exact": exact_search(query, source_id, limit, commit_sha),
        "vector": vector_search(query, source_id, limit, commit_sha),
        "structural": structural_search(query, source_id, limit, commit_sha),
        "semantic": semantic_search(query, source_id, limit, commit_sha),
    }
