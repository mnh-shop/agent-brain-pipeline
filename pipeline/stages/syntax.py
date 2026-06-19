from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes

from pipeline.analyzers.tree_sitter import analyze_file, detect_language
from pipeline.config import get_config
from pipeline.db import PIPELINE_VERSION, make_unit_id, record_stage_report, replace_units
from pipeline.urls import parse_repository_url
from pipeline.schemas.ids import normalize_path
from pipeline.util import ignored, sha256_bytes, sha256_file, write_json


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _decode_text(path: Path, max_bytes: int) -> tuple[str | None, str | None, bool, str]:
    raw = path.read_bytes()
    if b"\x00" in raw or len(raw) > max_bytes:
        return None, None, True, "application/octet-stream"
    match = from_bytes(raw).best()
    if not match:
        return None, None, True, "application/octet-stream"
    text = str(match)
    if not text and raw:
        return None, str(match.encoding or "utf-8"), True, "application/octet-stream"
    return text, str(match.encoding or "utf-8"), False, "text/plain"


def _unit_from_symbol(run: dict[str, Any], symbol: dict[str, Any], oversized: bool, max_symbol_bytes: int) -> dict[str, Any]:
    content = str(symbol.get("content", ""))
    content_bytes = len(content.encode("utf-8"))
    unit_type = symbol["symbol_kind"]
    if oversized and content_bytes > max_symbol_bytes:
        unit_type = "code_chunk"
    content_sha256 = symbol.get("content_sha256") or sha256_bytes(content.encode("utf-8"))
    normalized = symbol.get("normalized_path") or normalize_path(symbol["path"])
    unit_id = make_unit_id(
        symbol["source_id"],
        symbol["commit_sha"],
        normalized,
        unit_type,
        symbol.get("start_line"),
        symbol.get("end_line"),
        content_sha256,
    )
    payload = {
        "unit_id": unit_id,
        "source_id": symbol["source_id"],
        "platform": symbol["platform"],
        "repository_url": symbol["repository_url"],
        "namespace": symbol["namespace"],
        "repository_name": symbol["repository_name"],
        "requested_ref": symbol.get("requested_ref"),
        "resolved_branch": symbol.get("resolved_branch"),
        "commit_sha": symbol["commit_sha"],
        "path": symbol["path"],
        "normalized_path": normalized,
        "unit_type": unit_type,
        "heading": symbol.get("qualified_name"),
        "language": symbol.get("language"),
        "start_line": symbol.get("start_line"),
        "end_line": symbol.get("end_line"),
        "start_byte": symbol.get("start_byte"),
        "end_byte": symbol.get("end_byte"),
        "file_sha256": symbol.get("file_sha256"),
        "content_sha256": content_sha256,
        "content": content,
        "generator_name": "syntax",
        "generator_version": "1",
        "schema_version": 1,
        "pipeline_version": symbol["pipeline_version"],
        "source_line_start": symbol.get("start_line"),
        "source_line_end": symbol.get("end_line"),
        "source_byte_start": symbol.get("start_byte"),
        "source_byte_end": symbol.get("end_byte"),
        "metadata": {
            "kind": symbol["symbol_kind"],
            "qualified_name": symbol["qualified_name"],
            "symbol_id": symbol["symbol_id"],
            "source": "tree-sitter" if not oversized else "tree-sitter-oversized-fallback",
        },
    }
    return payload


def _fallback_file_unit(run: dict[str, Any], path: Path, text: str, language: str, reason: str) -> dict[str, Any]:
    lines = text.splitlines()
    end_line = len(lines) or 1
    content = text if text else ""
    content_hash = sha256_bytes(content.encode("utf-8"))
    normalized = normalize_path(str(path))
    return {
        "unit_id": make_unit_id(run["source_id"], run["commit_sha"], normalized, "code_chunk", 1, end_line, content_hash),
        "source_id": run["source_id"],
        "platform": run["platform"],
        "repository_url": run["repository_url"],
        "namespace": run["namespace"],
        "repository_name": run["repository_name"],
        "requested_ref": run.get("requested_ref"),
        "resolved_branch": run.get("resolved_branch"),
        "commit_sha": run["commit_sha"],
        "path": str(path),
        "normalized_path": normalized,
        "unit_type": "code_chunk",
        "heading": None,
        "language": language,
        "start_line": 1,
        "end_line": end_line,
        "start_byte": 0,
        "end_byte": len(content.encode("utf-8")),
        "file_sha256": sha256_file(path),
        "content_sha256": content_hash,
        "content": content,
        "generator_name": "syntax:fallback",
        "generator_version": "1",
        "schema_version": 1,
        "pipeline_version": run["pipeline_version"],
        "source_line_start": 1,
        "source_line_end": end_line,
        "source_byte_start": 0,
        "source_byte_end": len(content.encode("utf-8")),
        "metadata": {"reason": reason, "language": language},
    }


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    section = cfg.get("syntax", {})
    pipeline_cfg = cfg.get("pipeline", {})
    snapshot = Path(run["snapshot_path"])
    repo = parse_repository_url(run["repository_url"])
    syntax_dir = snapshot.parent / "syntax"
    syntax_dir.mkdir(parents=True, exist_ok=True)

    max_text_bytes = int(pipeline_cfg.get("max_text_file_bytes", 5 * 1024 * 1024))
    max_symbol_bytes = int(section.get("max_symbol_bytes", 256 * 1024))

    files: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    imports: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    languages: dict[str, Any] = {
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "tree_sitter_package": "tree-sitter-language-pack",
        "tree_sitter_version": None,
        "files": [],
    }

    for path in sorted(snapshot.rglob("*")):
        if not path.is_file():
            continue
        if ignored(path, snapshot, list(pipeline_cfg.get("ignored_directories", [])), list(pipeline_cfg.get("ignored_globs", []))):
            continue
        relative_path = path.relative_to(snapshot)
        language = detect_language(path)
        file_record = {
            "path": relative_path.as_posix(),
            "normalized_path": normalize_path(relative_path.as_posix()),
            "language": language,
            "size_bytes": path.stat().st_size,
            "file_sha256": sha256_file(path),
        }
        files.append(file_record)
        text, encoding, is_binary, mime_type = _decode_text(path, max_text_bytes)
        if text is None:
            parse_errors.append({
                "path": relative_path.as_posix(),
                "language": language,
                "kind": "binary_or_oversized",
                "message": "File could not be decoded as text",
                "file_sha256": file_record["file_sha256"],
            })
            continue
        metadata = {
            "run_id": run["run_id"],
            "source_id": run["source_id"],
            "platform": repo.platform,
            "repository_url": run["repository_url"],
            "namespace": repo.namespace,
            "repository_name": repo.name,
            "requested_ref": run.get("requested_ref"),
            "resolved_branch": run.get("resolved_branch"),
            "commit_sha": run["commit_sha"],
            "file_sha256": file_record["file_sha256"],
            "content_sha256": sha256_bytes(text.encode("utf-8")),
            "pipeline_version": PIPELINE_VERSION,
            "file_encoding": encoding,
            "mime_type": mime_type,
        }
        analysis = analyze_file(relative_path, text, metadata, cfg)
        if analysis.tree_sitter_version and not languages["tree_sitter_version"]:
            languages["tree_sitter_version"] = analysis.tree_sitter_version
        languages["files"].append({"path": relative_path.as_posix(), "language": analysis.language, "parser": analysis.parser_name, "passed": analysis.passed})
        if analysis.parse_errors:
            parse_errors.extend([{**error, "path": relative_path.as_posix(), "language": analysis.language} for error in analysis.parse_errors])
        symbols.extend(analysis.symbols)
        imports.extend(analysis.imports)
        nodes.extend(analysis.nodes)

        if analysis.fallback_units:
            units.extend(analysis.fallback_units)
            continue

        oversized_symbol_count = 0
        for symbol in analysis.symbols:
            content = str(symbol.get("content", ""))
            if len(content.encode("utf-8")) > max_symbol_bytes:
                oversized_symbol_count += 1
            units.append(_unit_from_symbol(run, symbol, True, max_symbol_bytes))

        if oversized_symbol_count:
            parse_errors.append({
                "path": str(path),
                "language": analysis.language,
                "kind": "oversized_symbol",
                "message": f"{oversized_symbol_count} symbol(s) exceeded max_symbol_bytes={max_symbol_bytes}",
            })

    _write_jsonl(syntax_dir / "symbols.jsonl", symbols)
    _write_jsonl(syntax_dir / "imports.jsonl", imports)
    _write_jsonl(syntax_dir / "syntax-nodes.jsonl", nodes)
    _write_jsonl(syntax_dir / "parse-errors.jsonl", parse_errors)
    write_json(syntax_dir / "languages.json", languages)

    report = {
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "passed": True,
        "tree_sitter_package": languages["tree_sitter_package"],
        "tree_sitter_version": languages["tree_sitter_version"],
        "file_count": len(files),
        "symbol_count": len(symbols),
        "import_count": len(imports),
        "parse_failure_count": len(parse_errors),
        "fallback_unit_count": len([unit for unit in units if unit["unit_type"] == "code_chunk"]),
        "symbol_unit_count": len([unit for unit in units if unit["unit_type"] != "code_chunk"]),
        "files": files,
        "parse_errors": parse_errors,
    }

    write_json(syntax_dir / "syntax-report.json", report)
    (syntax_dir / "syntax-report.md").write_text(
        "\n".join(
            [
                "# Syntax report",
                "",
                f"- Files: {report['file_count']}",
                f"- Symbols: {report['symbol_count']}",
                f"- Imports: {report['import_count']}",
                f"- Parse failures: {report['parse_failure_count']}",
                f"- Fallback units: {report['fallback_unit_count']}",
                f"- Symbol units: {report['symbol_unit_count']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    replace_units(run["run_id"], units, symbols=symbols, imports=imports)
    record_stage_report(
        {
            "run_id": run["run_id"],
            "stage": "syntax",
            "source_id": run["source_id"],
            "commit_sha": run["commit_sha"],
            "status": "passed",
            "passed": True,
            "summary": {"files": len(files), "symbols": len(symbols), "imports": len(imports)},
            "metrics": report,
            "warnings": [f"{len(parse_errors)} parse failures recorded"] if parse_errors else [],
            "errors": [],
            "schema_version": 1,
            "pipeline_version": PIPELINE_VERSION,
        }
    )
    return report
