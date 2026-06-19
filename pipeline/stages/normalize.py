from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.db import replace_files, replace_units
from pipeline.urls import parse_repository_url
from pipeline.util import ignored, read_json, write_json

from pipeline.stages._normalize import (
    code_units,
    decode_file,
    is_markdown,
    markdown_units,
    supported_language,
)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _file_record(*, run: dict[str, Any], path: Path, relative: str, raw: bytes, text: str | None, encoding: str | None, is_binary: bool, mime: str, duplicate_of: str | None, pipeline_version: str) -> dict[str, Any]:
    from pipeline.util import sha256_bytes

    digest = sha256_bytes(raw)
    lines = text.splitlines() if text is not None else []
    return {
        "schema_version": 1,
        "pipeline_version": pipeline_version,
        "source_id": run["source_id"],
        "platform": run.get("platform", "github"),
        "repository_url": run["repository_url"],
        "namespace": run.get("namespace", ""),
        "repository_name": run.get("repository_name", ""),
        "requested_ref": run.get("requested_ref"),
        "resolved_branch": run.get("resolved_branch"),
        "commit_sha": run["commit_sha"],
        "path": relative,
        "sha256": digest,
        "file_sha256": digest,
        "content_sha256": digest if not is_binary else None,
        "content_hash": digest if not is_binary else None,
        "source_line_start": 1 if lines else None,
        "source_line_end": len(lines) if lines else None,
        "source_byte_start": 0 if raw else None,
        "source_byte_end": len(raw) if raw else None,
        "parser_name": "normalize",
        "parser_version": "1",
        "size_bytes": len(raw),
        "mime_type": mime,
        "encoding": encoding,
        "is_binary": is_binary,
        "duplicate_of": duplicate_of,
        "provenance": {
            "source_id": run["source_id"],
            "platform": run.get("platform", "github"),
            "repository_url": run["repository_url"],
            "namespace": run.get("namespace", ""),
            "repository_name": run.get("repository_name", ""),
            "requested_ref": run.get("requested_ref"),
            "resolved_branch": run.get("resolved_branch"),
            "commit_sha": run["commit_sha"],
            "path": relative,
            "file_sha256": digest,
            "content_sha256": digest if not is_binary else None,
            "source_line_start": 1 if lines else None,
            "source_line_end": len(lines) if lines else None,
            "source_byte_start": 0 if raw else None,
            "source_byte_end": len(raw) if raw else None,
            "parser_name": "normalize",
            "parser_version": "1",
            "schema_version": 1,
            "pipeline_version": pipeline_version,
        },
    }


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    snapshot = Path(run["snapshot_path"])
    repo = parse_repository_url(run["repository_url"])
    run = {
        **run,
        "platform": repo.platform,
        "namespace": repo.namespace,
        "repository_name": repo.name,
        "repository_url": repo.normalized,
    }
    max_bytes = int(cfg["pipeline"].get("max_text_file_bytes", 5 * 1024 * 1024))
    code_lines = int(cfg["pipeline"].get("code_chunk_lines", 180))
    overlap = int(cfg["pipeline"].get("code_chunk_overlap_lines", 20))
    markdown_chars = int(cfg["pipeline"].get("markdown_chunk_characters", 12000))
    ignored_dirs = list(cfg["pipeline"].get("ignored_directories", []))
    ignored_globs = list(cfg["pipeline"].get("ignored_globs", []))
    pipeline_version = "0.1.0"

    files: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    encodings: dict[str, int] = {}
    binary_count = 0
    text_count = 0

    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() or ignored(path, snapshot, ignored_dirs, ignored_globs):
            continue
        relative = path.relative_to(snapshot).as_posix()
        try:
            text, encoding, is_binary, mime, raw = decode_file(path, max_bytes)
            from pipeline.util import sha256_bytes

            digest = sha256_bytes(raw)
            duplicate_of = seen_hashes.get(digest)
            if not duplicate_of:
                seen_hashes[digest] = relative
            file_row = _file_record(
                run=run,
                path=path,
                relative=relative,
                raw=raw,
                text=text,
                encoding=encoding,
                is_binary=is_binary,
                mime=mime,
                duplicate_of=duplicate_of,
                # provenance source identity
                pipeline_version=pipeline_version,
            )
            files.append(file_row)
            if is_binary or text is None:
                binary_count += 1
                continue
            text_count += 1
            encodings[encoding or "unknown"] = encodings.get(encoding or "unknown", 0) + 1
            language = supported_language(path)
            if is_markdown(path):
                units.extend(
                    markdown_units(
                        text=text,
                        raw=raw,
                        path=relative,
                        source_id=run["source_id"],
                        platform=repo.platform,
                        repository_url=repo.normalized,
                        namespace=repo.namespace,
                        repository_name=repo.name,
                        requested_ref=run.get("requested_ref"),
                        resolved_branch=run.get("resolved_branch"),
                        commit_sha=run["commit_sha"],
                        file_sha256=digest,
                        pipeline_version=pipeline_version,
                        max_chars=markdown_chars,
                    )
                )
            else:
                units.extend(
                    code_units(
                        text=text,
                        raw=raw,
                        path=relative,
                        source_id=run["source_id"],
                        platform=repo.platform,
                        repository_url=repo.normalized,
                        namespace=repo.namespace,
                        repository_name=repo.name,
                        requested_ref=run.get("requested_ref"),
                        resolved_branch=run.get("resolved_branch"),
                        commit_sha=run["commit_sha"],
                        file_sha256=digest,
                        language=language,
                        pipeline_version=pipeline_version,
                        lines_per_chunk=code_lines,
                        overlap=overlap,
                    )
                )
        except Exception as exc:
            errors.append({"path": relative, "error": str(exc)})

    files_jsonl = snapshot.parent / "files.jsonl"
    units_jsonl = snapshot.parent / "units.jsonl"
    _write_jsonl(files_jsonl, files)
    _write_jsonl(units_jsonl, units)
    replace_files(run["run_id"], files)
    replace_units(run["run_id"], units)
    report = {
        "schema_version": 1,
        "pipeline_version": pipeline_version,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "snapshot_path": str(snapshot),
        "files_jsonl": str(files_jsonl),
        "units_jsonl": str(units_jsonl),
        "file_count": len(files),
        "text_file_count": text_count,
        "binary_file_count": binary_count,
        "duplicate_file_count": sum(1 for row in files if row.get("duplicate_of")),
        "unit_count": len(units),
        "encodings": encodings,
        "errors": errors,
        "passed": not errors,
    }
    report_path = snapshot.parent / "normalization-report.json"
    write_json(report_path, report)
    (snapshot.parent / "normalization-report.md").write_text(
        "\n".join([
            "# Normalization Report",
            "",
            f"- Run: `{run['run_id']}`",
            f"- Commit: `{run['commit_sha']}`",
            f"- Passed: `{report['passed']}`",
            f"- Files: {len(files)}",
            f"- Units: {len(units)}",
        ]) + "\n",
        encoding="utf-8",
    )
    return report
