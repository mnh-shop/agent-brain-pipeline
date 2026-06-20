from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.db import PIPELINE_VERSION, make_symbol_id
from pipeline.schemas.ids import normalize_path, stable_hash
from pipeline.util import read_json, sha256_file, sha256_text, write_json


RELATIONS = {
    "defines": "DEFINES",
    "contains": "CONTAINS",
    "imports": "IMPORTS",
    "calls": "CALLS",
    "extends": "EXTENDS",
    "implements": "IMPLEMENTS",
    "depends_on": "DEPENDS_ON",
    "exports": "EXPORTS",
}


@dataclass(frozen=True)
class ToolProbe:
    executable: str
    version: str | None
    executable_hash: str | None
    help_text: str | None
    supported_commands: list[str]


def _text_or_none(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


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
        for candidate in ("config", "db", "kuzudb", "index", "list", "stats", "bundle", "export", "find", "query"):
            if candidate in help_text:
                supported_commands.append(candidate)
    return ToolProbe(command, version, executable_hash, help_text, supported_commands)


def _jsonl_write(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _parse_stats(text: str) -> dict[str, Any]:
    values: dict[str, Any] = {"raw": text}
    for pattern, key in (
        (r"nodes?\D+(\d+)", "node_count"),
        (r"edges?\D+(\d+)", "edge_count"),
        (r"symbols?\D+(\d+)", "symbol_count"),
    ):
        import re

        match = re.search(pattern, text, flags=re.I)
        if match:
            values[key] = int(match.group(1))
    return values


def _parse_list(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
                continue
            except Exception:
                pass
        rows.append({"raw": line})
    return rows


def _syntax_index(snapshot: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    syntax_dir = snapshot.parent / "syntax"
    symbol_rows = _load_jsonl(syntax_dir / "symbols.jsonl")
    file_rows = []
    normalization_dir = snapshot.parent / "normalization"
    if (normalization_dir / "files.jsonl").exists():
        file_rows = _load_jsonl(normalization_dir / "files.jsonl")
    symbols_by_id = {row["symbol_id"]: row for row in symbol_rows if "symbol_id" in row}
    files_by_path = {normalize_path(row.get("path", "")): row for row in file_rows if row.get("path")}
    return symbols_by_id, files_by_path, symbol_rows


def _file_unit_node(file_row: dict[str, Any]) -> dict[str, Any]:
    path = file_row["path"]
    normalized = file_row.get("normalized_path") or normalize_path(path)
    return {
        "node_id": stable_hash("file", normalized, file_row.get("file_sha256") or file_row.get("sha256") or ""),
        "kind": "file",
        "label": path,
        "source_id": file_row.get("source_id"),
        "commit_sha": file_row.get("commit_sha"),
        "path": path,
        "normalized_path": normalized,
        "file_sha256": file_row.get("file_sha256") or file_row.get("sha256"),
        "content_sha256": file_row.get("content_sha256") or file_row.get("sha256"),
        "start_line": file_row.get("source_line_start"),
        "end_line": file_row.get("source_line_end"),
        "start_byte": file_row.get("source_byte_start"),
        "end_byte": file_row.get("source_byte_end"),
        "schema_version": 1,
    }


def _symbol_node(symbol: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": symbol["symbol_id"],
        "kind": "symbol",
        "label": symbol.get("qualified_name") or symbol["path"],
        "source_id": symbol["source_id"],
        "commit_sha": symbol["commit_sha"],
        "path": symbol["path"],
        "normalized_path": symbol.get("normalized_path") or normalize_path(symbol["path"]),
        "symbol_kind": symbol["symbol_kind"],
        "language": symbol["language"],
        "qualified_name": symbol["qualified_name"],
        "start_line": symbol.get("start_line"),
        "end_line": symbol.get("end_line"),
        "start_byte": symbol.get("start_byte"),
        "end_byte": symbol.get("end_byte"),
        "file_sha256": symbol.get("file_sha256"),
        "content_sha256": symbol.get("content_sha256"),
        "schema_version": 1,
    }


def _edge(source: str, target: str, relation: str, source_id: str, commit_sha: str, path: str | None = None, note: str | None = None) -> dict[str, Any]:
    return {
        "edge_id": stable_hash(source, target, relation, path or ""),
        "source_node_id": source,
        "target_node_id": target,
        "relation": relation,
        "source_id": source_id,
        "commit_sha": commit_sha,
        "path": path,
        "note": note,
        "schema_version": 1,
    }


def normalize_graph(
    *,
    run: dict[str, Any],
    snapshot: Path,
    raw_dir: Path,
    normalized_dir: Path,
    probe: ToolProbe,
    command_results: dict[str, dict[str, Any]],
    syntax_symbols: list[dict[str, Any]],
    syntax_files: list[dict[str, Any]],
) -> dict[str, Any]:
    normalized_dir.mkdir(parents=True, exist_ok=True)
    symbols_by_id, files_by_path, _ = _syntax_index(snapshot)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    symbol_matches: list[dict[str, Any]] = []
    unmatched_nodes: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    symbols_by_path: dict[str, list[dict[str, Any]]] = {}
    symbols_by_path_and_name: dict[tuple[str, str], dict[str, Any]] = {}

    for file_row in syntax_files:
        nodes.append(_file_unit_node(file_row))

    for symbol in syntax_symbols:
        nodes.append(_symbol_node(symbol))
        normalized_path = symbol.get("normalized_path") or normalize_path(symbol["path"])
        symbols_by_path.setdefault(normalized_path, []).append(symbol)
        qualified_name = symbol.get("qualified_name")
        if qualified_name:
            symbols_by_path_and_name[(normalized_path, qualified_name)] = symbol

    for file_row in syntax_files:
        file_node_id = stable_hash("file", file_row.get("normalized_path") or normalize_path(file_row["path"]), file_row.get("file_sha256") or file_row.get("sha256") or "")
        for symbol in symbols_by_path.get(normalize_path(file_row["path"]), []):
            symbol_node_id = symbol["symbol_id"]
            edges.append(_edge(file_node_id, symbol_node_id, "CONTAINS", run["source_id"], run["commit_sha"], file_row["path"]))

    for symbol in syntax_symbols:
        if symbol["symbol_kind"] in {"class", "function", "method", "constructor", "interface", "enum", "configuration-object", "constant", "module"}:
            symbol_matches.append({
                "symbol_id": symbol["symbol_id"],
                "node_id": symbol["symbol_id"],
                "matched": True,
                "reason": "canonical_symbol",
                "path": symbol["path"],
                "normalized_path": symbol.get("normalized_path") or normalize_path(symbol["path"]),
                "qualified_name": symbol.get("qualified_name"),
                "symbol_kind": symbol["symbol_kind"],
                "start_line": symbol.get("start_line"),
                "end_line": symbol.get("end_line"),
                "start_byte": symbol.get("start_byte"),
                "end_byte": symbol.get("end_byte"),
                "content_sha256": symbol.get("content_sha256"),
                "file_sha256": symbol.get("file_sha256"),
                "schema_version": 1,
            })

    raw_nodes_path = raw_dir / "nodes.jsonl"
    raw_edges_path = raw_dir / "edges.jsonl"
    raw_queries_path = raw_dir / "queries.jsonl"
    if raw_nodes_path.exists():
        raw_nodes = _load_jsonl(raw_nodes_path)
        for row in raw_nodes:
            raw_node_id = row.get("node_id") or stable_hash(row.get("label"), row.get("kind"), row.get("path"))
            matched = None
            normalized_path = normalize_path(row["normalized_path"]) if row.get("normalized_path") else ""
            qualified_name = row.get("qualified_name")
            if normalized_path and qualified_name:
                symbol = symbols_by_path_and_name.get((normalized_path, qualified_name))
                if symbol:
                    matched = symbol["symbol_id"]
            if matched:
                symbol_matches.append({**row, "matched": True, "matched_symbol_id": matched, "reason": "raw-node-matched"})
            else:
                unmatched_nodes.append({**row, "node_id": raw_node_id, "matched": False, "reason": row.get("reason") or "unmatched_raw_node"})
    else:
        for symbol in syntax_symbols:
            if not symbol.get("qualified_name"):
                unmatched_nodes.append({"node_id": symbol["symbol_id"], "reason": "missing_qualified_name", "path": symbol["path"]})

    if raw_edges_path.exists():
        for row in _load_jsonl(raw_edges_path):
            edges.append(row)
    else:
        for symbol in syntax_symbols:
            if symbol["symbol_kind"] != "module":
                edges.append(_edge(symbol["symbol_id"], symbol["symbol_id"], "DEFINES", run["source_id"], run["commit_sha"], symbol["path"]))

    if raw_queries_path.exists():
        queries.extend(_load_jsonl(raw_queries_path))

    for name, result in command_results.items():
        queries.append({
            "query_name": name,
            "command": result["command"],
            "returncode": result["returncode"],
            "stdout_path": result["stdout_path"],
            "stderr_path": result["stderr_path"],
            "passed": result["returncode"] == 0,
        })

    manifest = {
        "schema_version": 1,
        "pipeline_version": run.get("pipeline_version") or PIPELINE_VERSION,
        "codegraph_version": probe.version,
        "executable": probe.executable,
        "executable_hash": probe.executable_hash,
        "supported_commands": probe.supported_commands,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "symbol_match_count": len(symbol_matches),
        "unmatched_count": len(unmatched_nodes),
        "query_count": len(queries),
    }

    _jsonl_write(normalized_dir / "nodes.jsonl", nodes)
    _jsonl_write(normalized_dir / "edges.jsonl", edges)
    _jsonl_write(normalized_dir / "symbol-matches.jsonl", symbol_matches)
    _jsonl_write(normalized_dir / "unmatched-nodes.jsonl", unmatched_nodes)
    _jsonl_write(normalized_dir / "queries.jsonl", queries)
    write_json(normalized_dir / "manifest.json", manifest)
    return manifest
