from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.schemas.ids import normalize_path, stable_hash
from pipeline.util import read_json, sha256_file, sha256_text, write_json


@dataclass(frozen=True)
class ToolProbe:
    executable: str
    version: str | None
    executable_hash: str | None
    help_text: str | None
    supported_commands: list[str]


def probe_tool(command: str, env: dict[str, str], timeout: int) -> ToolProbe:
    from pipeline.util import run_command

    exe_path = shutil.which(command)
    executable_hash = sha256_file(Path(exe_path)) if exe_path and Path(exe_path).exists() else None
    version = None
    help_text = None
    supported_commands: list[str] = []

    version_result = run_command([command, "--version"], env=env, timeout=timeout, check=False)
    if version_result.returncode == 0 and version_result.stdout.strip():
        version = version_result.stdout.strip().splitlines()[0].strip()
    help_result = run_command([command, "--help"], env=env, timeout=timeout, check=False)
    if help_result.returncode == 0 and help_result.stdout.strip():
        help_text = help_result.stdout
        for candidate in ("cli", "index_repository", "list_projects", "get_architecture", "search_graph", "semantic_query"):
            if candidate in help_text:
                supported_commands.append(candidate)
    return ToolProbe(command, version, executable_hash, help_text, supported_commands)


def _json_output(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value[-20000:]


def _json_lines(value: str) -> list[Any]:
    rows = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
    return rows


def _coerce_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        return _json_output(value)
    return value


def _project_from_index(index_output: Any, fallback: str) -> str:
    if isinstance(index_output, dict):
        for key in ("project", "project_id", "project_name", "name"):
            value = index_output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in index_output.values():
            found = _project_from_index(value, "")
            if found:
                return found
    if isinstance(index_output, list):
        for value in index_output:
            found = _project_from_index(value, "")
            if found:
                return found
    return fallback


def parse_projects_output(value: str | Any) -> list[dict[str, Any]]:
    parsed = value if not isinstance(value, str) else _json_output(value)
    if isinstance(parsed, dict):
        for key in ("projects", "results", "items", "data"):
            inner = parsed.get(key)
            if isinstance(inner, list):
                return [item if isinstance(item, dict) else {"value": item} for item in inner]
        return [parsed]
    if isinstance(parsed, list):
        return [item if isinstance(item, dict) else {"value": item} for item in parsed]
    return [{"value": parsed}]


def _line_range_ok(start: int | None, end: int | None, text: str) -> bool:
    if start is None or end is None:
        return True
    lines = text.splitlines()
    return 1 <= start <= end <= max(len(lines), 1)


def _map_to_symbol(
    path: str | None,
    symbol_kind: str | None,
    qualified_name: str | None,
    start_line: int | None,
    end_line: int | None,
    content_sha256: str | None,
    symbols_by_path: dict[str, list[dict[str, Any]]],
    units_by_path: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    normalized = normalize_path(path or "")
    candidates = symbols_by_path.get(normalized, [])
    for row in candidates:
        if qualified_name and row.get("qualified_name") != qualified_name:
            continue
        if symbol_kind and row.get("symbol_kind") != symbol_kind and row.get("unit_type") != symbol_kind:
            continue
        if start_line is not None and row.get("start_line") not in {None, start_line}:
            continue
        if end_line is not None and row.get("end_line") not in {None, end_line}:
            continue
        if content_sha256 and row.get("content_sha256") not in {None, content_sha256}:
            continue
        return {"matched": True, "symbol_id": row.get("symbol_id"), "unit_id": row.get("unit_id"), "reason": "matched_symbol"}
    for row in units_by_path.get(normalized, []):
        if start_line is not None and row.get("start_line") not in {None, start_line}:
            continue
        if end_line is not None and row.get("end_line") not in {None, end_line}:
            continue
        if content_sha256 and row.get("content_sha256") not in {None, content_sha256}:
            continue
        return {"matched": True, "unit_id": row.get("unit_id"), "reason": "matched_unit"}
    return {"matched": False, "reason": "unmatched"}


def _index_canonical(snapshot: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    syntax_dir = snapshot.parent / "syntax"
    normalization_dir = snapshot.parent / "normalization"
    symbols = []
    if (syntax_dir / "symbols.jsonl").exists():
        symbols = [json.loads(line) for line in (syntax_dir / "symbols.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    units = []
    if (normalization_dir / "units.jsonl").exists():
        units = [json.loads(line) for line in (normalization_dir / "units.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    symbols_by_path: dict[str, list[dict[str, Any]]] = {}
    units_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in symbols:
        symbols_by_path.setdefault(normalize_path(row.get("path", "")), []).append(row)
    for row in units:
        units_by_path.setdefault(normalize_path(row.get("path", "")), []).append(row)
    return symbols_by_path, units_by_path


def _record_from_semantic_item(
    item: dict[str, Any],
    *,
    source_id: str,
    commit_sha: str,
    project: str,
    snapshot: Path,
    symbols_by_path: dict[str, list[dict[str, Any]]],
    units_by_path: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    path = item.get("path") or item.get("file_path") or item.get("location", {}).get("path")
    start_line = item.get("start_line") or item.get("line_start") or item.get("location", {}).get("start_line")
    end_line = item.get("end_line") or item.get("line_end") or item.get("location", {}).get("end_line")
    content = item.get("content") or item.get("excerpt") or ""
    content_sha256 = item.get("content_sha256") or (sha256_text(content) if content else None)
    mapping = _map_to_symbol(path, item.get("symbol_kind") or item.get("kind"), item.get("qualified_name") or item.get("name"), start_line, end_line, content_sha256, symbols_by_path, units_by_path)
    exact_commit = item.get("commit_sha") in {None, commit_sha}
    return {
        "item_id": stable_hash(project, path or "", start_line, end_line, content_sha256 or "", item.get("qualified_name") or item.get("name") or ""),
        "project": project,
        "source_id": source_id,
        "commit_sha": commit_sha,
        "path": path,
        "normalized_path": normalize_path(path or "") if path else None,
        "start_line": start_line,
        "end_line": end_line,
        "start_byte": item.get("start_byte"),
        "end_byte": item.get("end_byte"),
        "qualified_name": item.get("qualified_name") or item.get("name"),
        "symbol_kind": item.get("symbol_kind") or item.get("kind"),
        "content_sha256": content_sha256,
        "matched": mapping["matched"],
        "matched_symbol_id": mapping.get("symbol_id"),
        "matched_unit_id": mapping.get("unit_id"),
        "reason": mapping["reason"],
        "exact_commit": exact_commit,
        "line_range_valid": _line_range_ok(_safe_int(start_line), _safe_int(end_line), content),
        "schema_version": 1,
    }


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def normalize_semantic_outputs(
    *,
    run: dict[str, Any],
    snapshot: Path,
    raw_dir: Path,
    normalized_dir: Path,
    index_output: Any,
    projects_output: Any,
    architecture_output: Any,
    graph_output: Any,
    semantic_output: Any,
    probe: ToolProbe,
    command_results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    normalized_dir.mkdir(parents=True, exist_ok=True)
    symbols_by_path, units_by_path = _index_canonical(snapshot)
    project = _project_from_index(index_output, str(snapshot.name))
    project_entry = None
    for item in parse_projects_output(projects_output):
        if any(str(item.get(key, "")).strip() == project for key in ("project", "project_id", "project_name", "name")):
            project_entry = item
            break
    arch_parsed = _coerce_json(architecture_output)
    graph_output = _coerce_json(graph_output)
    semantic_rows = _coerce_json(semantic_output)
    semantic_records: list[dict[str, Any]] = []
    graph_records: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    relationship_rows: list[dict[str, Any]] = []

    def handle_item(item: dict[str, Any], bucket: str) -> dict[str, Any]:
        record = _record_from_semantic_item(
            item,
            source_id=run["source_id"],
            commit_sha=run["commit_sha"],
            project=project,
            snapshot=snapshot,
            symbols_by_path=symbols_by_path,
            units_by_path=units_by_path,
        )
        if not record["matched"]:
            unmatched.append({"bucket": bucket, **record, "raw": item})
        else:
            symbol_rows.append(record)
        return record

    if isinstance(semantic_rows, dict) and any(k in semantic_rows for k in ("results", "items", "data")):
        semantic_items = semantic_rows.get("results") or semantic_rows.get("items") or semantic_rows.get("data") or []
    elif isinstance(semantic_rows, list):
        semantic_items = semantic_rows
    else:
        semantic_items = [semantic_rows]
    for item in semantic_items:
        if isinstance(item, dict):
            semantic_records.append(handle_item(item, "semantic"))

    if isinstance(graph_output, dict):
        graph_items = graph_output.get("results") or graph_output.get("items") or graph_output.get("data") or graph_output.get("edges") or graph_output.get("nodes") or []
        if isinstance(graph_items, dict):
            graph_items = [graph_items]
        elif isinstance(graph_items, list):
            graph_items = graph_items
        else:
            graph_items = [graph_items]
    elif isinstance(graph_output, list):
        graph_items = graph_output
    else:
        graph_items = [graph_output]
    for item in graph_items:
        if not isinstance(item, dict):
            continue
        record = handle_item(item, "graph")
        graph_records.append(record)
        relation = item.get("relation") or item.get("edge_type") or item.get("kind")
        if relation:
            relationship_rows.append(
                {
                    "relationship_id": stable_hash(project, record.get("path") or "", relation, record.get("qualified_name") or ""),
                    "project": project,
                    "source_id": run["source_id"],
                    "commit_sha": run["commit_sha"],
                    "relation": relation,
                    "path": record.get("path"),
                    "qualified_name": record.get("qualified_name"),
                    "source_symbol_id": record.get("matched_symbol_id"),
                    "target_symbol_id": item.get("target_symbol_id"),
                    "matched": record["matched"],
                    "exact_commit": record["exact_commit"],
                    "schema_version": 1,
                }
            )

    architecture_record = {
        "project": project,
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "raw": arch_parsed,
        "schema_version": 1,
    }
    project_record = {
        "project": project,
        "project_entry": project_entry,
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "index_output": index_output,
        "schema_version": 1,
    }

    def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    _write_jsonl(normalized_dir / "symbols.jsonl", symbol_rows)
    _write_jsonl(normalized_dir / "relationships.jsonl", relationship_rows)
    write_json(normalized_dir / "project.json", project_record)
    write_json(normalized_dir / "architecture.json", architecture_record)
    _write_jsonl(normalized_dir / "semantic-smoke-results.jsonl", semantic_records)
    _write_jsonl(normalized_dir / "graph-smoke-results.jsonl", graph_records)

    manifest = {
        "schema_version": 1,
        "pipeline_version": run["pipeline_version"],
        "project": project,
        "project_in_list_projects": bool(project_entry),
        "graph_artifact_exists": bool((raw_dir / "graph.db.zst").exists()),
        "graph_artifact_sha256": sha256_file(raw_dir / "graph.db.zst") if (raw_dir / "graph.db.zst").exists() else None,
        "index_mode": command_results["index"]["index_mode"],
        "workers": command_results["index"]["workers"],
        "cache_dir": command_results["index"]["cache_dir"],
        "version": probe.version,
        "node_count": len(graph_records),
        "semantic_count": len(semantic_records),
        "matched_count": len([row for row in symbol_rows if row["matched"]]),
        "unmatched_count": len(unmatched),
        "all_exact_commit": all(row["exact_commit"] for row in semantic_records + graph_records),
        "passed": bool(project_entry and architecture_record["raw"] and graph_records and semantic_records and any(row["matched"] for row in semantic_records + graph_records)),
    }
    write_json(normalized_dir / "manifest.json", manifest)
    return {
        "project": project,
        "project_record": project_record,
        "architecture": architecture_record,
        "semantic_records": semantic_records,
        "graph_records": graph_records,
        "symbol_records": symbol_rows,
        "relationship_records": relationship_rows,
        "unmatched_records": unmatched,
        "manifest": manifest,
    }
