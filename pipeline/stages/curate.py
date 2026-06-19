from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes

from pipeline.config import get_config
from pipeline.db import make_unit_id, record_stage_report, replace_files, replace_units
from pipeline.schemas.ids import normalize_path
from pipeline.urls import parse_repository_url
from pipeline.util import ignored, read_json, run_command, sha256_file, sha256_text, write_json

LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".toml": "toml", ".md": "markdown", ".rst": "rst", ".txt": "text",
    ".html": "html", ".css": "css", ".sql": "sql", ".proto": "protobuf", ".graphql": "graphql",
}


def _decode(path: Path, max_bytes: int) -> tuple[str | None, str | None, bool, str]:
    raw = path.read_bytes()
    try:
        import magic  # type: ignore

        mime = magic.from_buffer(raw[:8192], mime=True)
    except Exception:
        mime = None
    mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if b"\x00" in raw[:8192] or len(raw) > max_bytes:
        return None, None, True, mime
    match = from_bytes(raw).best()
    if not match:
        return None, None, True, mime
    text = str(match)
    if not text.strip() and raw:
        return None, str(match.encoding or "unknown"), True, mime
    return text, str(match.encoding or "utf-8"), False, mime


def _markdown_units(text: str, path: str, metadata: dict[str, Any], max_chars: int) -> list[dict[str, Any]]:
    lines = text.splitlines()
    if not lines:
        return []

    headings: list[dict[str, Any]] = []
    heading_stack: list[str] = []
    for index, line in enumerate(lines, start=1):
        match = re.match(r"^(#{1,6})\s+(.*)$", line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        while len(heading_stack) >= level:
            heading_stack.pop()
        heading_stack.append(title)
        headings.append({"line": index, "level": level, "title": title, "path": " / ".join(heading_stack)})

    if not headings:
        headings = [{"line": 1, "level": 1, "title": Path(path).name, "path": Path(path).name}]

    offsets: list[tuple[int, int]] = []
    byte_cursor = 0
    for line in lines:
        raw = (line + "\n").encode("utf-8")
        offsets.append((byte_cursor, byte_cursor + len(raw)))
        byte_cursor += len(raw)

    def _line_chunk(start_line: int, end_line: int, heading: str) -> dict[str, Any] | None:
        content_lines = lines[start_line - 1:end_line]
        content = "\n".join(content_lines)
        if not content.strip():
            return None
        start_byte = offsets[start_line - 1][0]
        end_byte = offsets[end_line - 1][1]
        content_hash = sha256_text(content)
        normalized = normalize_path(path)
        unit = {
            "unit_id": make_unit_id(
                metadata["source_id"],
                metadata["commit_sha"],
                normalized,
                "markdown_section",
                start_line,
                end_line,
                content_hash,
            ),
            "source_id": metadata["source_id"],
            "platform": metadata["platform"],
            "repository_url": metadata["repository_url"],
            "namespace": metadata["namespace"],
            "repository_name": metadata["repository_name"],
            "requested_ref": metadata.get("requested_ref"),
            "resolved_branch": metadata.get("resolved_branch"),
            "commit_sha": metadata["commit_sha"],
            "path": path,
            "normalized_path": normalized,
            "unit_type": "markdown_section",
            "heading": heading,
            "language": "markdown",
            "start_line": start_line,
            "end_line": end_line,
            "start_byte": start_byte,
            "end_byte": end_byte,
            "file_sha256": metadata["file_sha256"],
            "content_sha256": content_hash,
            "content": content,
            "generator_name": "curate:markdown",
            "generator_version": "1",
            "schema_version": 1,
            "pipeline_version": metadata["pipeline_version"],
            "source_line_start": start_line,
            "source_line_end": end_line,
            "source_byte_start": start_byte,
            "source_byte_end": end_byte,
            "metadata": metadata,
        }
        expected = "\n".join(lines[start_line - 1:end_line])
        if unit["content"] != expected:
            raise RuntimeError(f"Markdown unit content mismatch for {path}:{start_line}-{end_line}")
        return unit

    units: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        section_start = heading["line"]
        next_start = headings[index + 1]["line"] - 1 if index + 1 < len(headings) else len(lines)
        section_lines = list(range(section_start, next_start + 1))
        if not section_lines:
            continue
        chunk_start = section_start
        chunk_chars = 0
        last_line = section_start - 1
        for line_no in section_lines:
            line_text = lines[line_no - 1]
            line_chars = len(line_text) + 1
            if chunk_chars and chunk_chars + line_chars > max_chars and last_line >= chunk_start:
                unit = _line_chunk(chunk_start, last_line, heading["path"])
                if unit:
                    units.append(unit)
                chunk_start = line_no
                chunk_chars = 0
            chunk_chars += line_chars
            last_line = line_no
        if last_line >= chunk_start:
            unit = _line_chunk(chunk_start, last_line, heading["path"])
            if unit:
                units.append(unit)
    return units


def _code_units(text: str, path: str, metadata: dict[str, Any], language: str, lines_per_chunk: int, overlap: int) -> list[dict[str, Any]]:
    lines = text.splitlines()
    units = []
    step = max(1, lines_per_chunk - overlap)
    for start_index in range(0, len(lines), step):
        end_index = min(len(lines), start_index + lines_per_chunk)
        piece = "\n".join(lines[start_index:end_index])
        if piece:
            units.append(_unit(path, "code_chunk", None, start_index + 1, end_index, language, piece, metadata))
        if end_index >= len(lines):
            break
    return units


def _unit(path: str, unit_type: str, heading: str | None, start: int, end: int, language: str, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_path(path)
    content_hash = sha256_text(content)
    return {
        "unit_id": make_unit_id(metadata["source_id"], metadata["commit_sha"], normalized, unit_type, start, end, content_hash),
        "source_id": metadata["source_id"],
        "platform": metadata["platform"],
        "repository_url": metadata["repository_url"],
        "namespace": metadata["namespace"],
        "repository_name": metadata["repository_name"],
        "requested_ref": metadata.get("requested_ref"),
        "resolved_branch": metadata.get("resolved_branch"),
        "commit_sha": metadata["commit_sha"],
        "path": path,
        "normalized_path": normalized,
        "unit_type": unit_type,
        "heading": heading,
        "start_line": start,
        "end_line": end,
        "start_byte": None,
        "end_byte": None,
        "language": language,
        "file_sha256": metadata["file_sha256"],
        "content_sha256": content_hash,
        "content": content,
        "generator_name": "curate:code",
        "generator_version": "1",
        "schema_version": 1,
        "pipeline_version": metadata["pipeline_version"],
        "source_line_start": start,
        "source_line_end": end,
        "source_byte_start": None,
        "source_byte_end": None,
        "metadata": metadata,
    }


def _verify_raw(snapshot: Path) -> list[dict[str, Any]]:
    raw = snapshot.parent / "raw"
    manifest_path = raw / "source-manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"Missing acquisition manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    checks: list[dict[str, Any]] = []
    for name, expected in manifest.get("checksums", {}).items():
        target = raw / name
        actual = sha256_file(target) if target.exists() else None
        checks.append({"artifact": name, "expected": expected, "actual": actual, "passed": actual == expected})
    bundle = raw / "repository.bundle"
    mirror = Path(str(manifest.get("mirror_path", "")))
    if not mirror.exists():
        raise RuntimeError(f"Missing acquisition mirror needed to verify bundle: {mirror}")
    verified = run_command(["git", "--git-dir", str(mirror), "bundle", "verify", str(bundle)], check=False, timeout=300)
    checks.append({"artifact": "repository.bundle:git-verify", "passed": verified.returncode == 0, "stderr": verified.stderr[-4000:]})
    failures = [item["artifact"] for item in checks if not item["passed"]]
    if failures:
        raise RuntimeError("Raw integrity verification failed: " + ", ".join(failures))
    return checks


def _broken_markdown_links(text: str, file_path: Path, snapshot: Path) -> list[str]:
    broken: list[str] = []
    for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
        target = target.strip().split()[0].strip("<>")
        if not target or target.startswith(("http://", "https://", "mailto:", "#", "data:")):
            continue
        relative = target.split("#", 1)[0].split("?", 1)[0]
        if not relative:
            continue
        candidate = (file_path.parent / relative).resolve()
        try:
            candidate.relative_to(snapshot.resolve())
        except ValueError:
            broken.append(target)
            continue
        if not candidate.exists():
            broken.append(target)
    return broken


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    snapshot = Path(run["snapshot_path"])
    if not snapshot.exists():
        raise RuntimeError(f"Snapshot does not exist: {snapshot}")
    integrity_checks = _verify_raw(snapshot)

    ignored_dirs = list(cfg["pipeline"].get("ignored_directories", []))
    ignored_globs = list(cfg["pipeline"].get("ignored_globs", []))
    max_bytes = int(cfg["pipeline"].get("max_text_file_bytes", 5 * 1024 * 1024))
    code_lines = int(cfg["pipeline"].get("code_chunk_lines", 180))
    overlap = int(cfg["pipeline"].get("code_chunk_overlap_lines", 20))
    markdown_chars = int(cfg["pipeline"].get("markdown_chunk_characters", 12000))

    files: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    encodings: dict[str, int] = {}
    binary_count = 0
    text_count = 0
    broken_links: list[dict[str, Any]] = []
    repo = parse_repository_url(run["repository_url"])

    metadata_base = {
        "source_id": run["source_id"],
        "platform": repo.platform,
        "repository_url": repo.normalized,
        "namespace": repo.namespace,
        "repository_name": repo.name,
        "requested_ref": run.get("requested_ref"),
        "resolved_branch": run.get("resolved_branch") or run.get("requested_ref"),
        "commit_sha": run["commit_sha"],
        "pipeline_version": "0.1.0",
    }

    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() or ignored(path, snapshot, ignored_dirs, ignored_globs):
            continue
        relative = path.relative_to(snapshot).as_posix()
        try:
            digest = sha256_file(path)
            text, encoding, is_binary, mime = _decode(path, max_bytes)
            duplicate_of = seen_hashes.get(digest)
            if not duplicate_of:
                seen_hashes[digest] = relative
            row = {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": digest,
                "file_sha256": digest,
                "content_sha256": None,
                "mime_type": mime,
                "encoding": encoding,
                "is_binary": is_binary,
                "duplicate_of": duplicate_of,
                "source_id": run["source_id"],
                "platform": repo.platform,
                "repository_url": repo.normalized,
                "namespace": repo.namespace,
                "repository_name": repo.name,
                "requested_ref": run.get("requested_ref"),
                "resolved_branch": run.get("resolved_branch") or run.get("requested_ref"),
                "commit_sha": run["commit_sha"],
                "source_line_start": None,
                "source_line_end": None,
                "source_byte_start": None,
                "source_byte_end": None,
                "generator_name": "curate:file",
                "generator_version": "1",
                "schema_version": 1,
                "pipeline_version": "0.1.0",
            }
            files.append(row)
            if is_binary or text is None:
                binary_count += 1
                continue
            text_count += 1
            encodings[encoding or "unknown"] = encodings.get(encoding or "unknown", 0) + 1
            language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")
            metadata = {**metadata_base, "file_sha256": digest, "mime_type": mime, "encoding": encoding}
            if language == "markdown":
                for target in _broken_markdown_links(text, path, snapshot):
                    broken_links.append({"path": relative, "target": target})
                units.extend(_markdown_units(text, relative, metadata, markdown_chars))
            else:
                units.extend(_code_units(text, relative, metadata, language, code_lines, overlap))
        except Exception as exc:
            errors.append({"path": relative, "error": str(exc)})

    replace_files(run["run_id"], files)
    replace_units(run["run_id"], units)
    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "snapshot_path": str(snapshot),
        "file_count": len(files),
        "text_file_count": text_count,
        "binary_file_count": binary_count,
        "duplicate_file_count": sum(1 for row in files if row.get("duplicate_of")),
        "unit_count": len(units),
        "encodings": encodings,
        "raw_integrity_checks": integrity_checks,
        "broken_relative_markdown_links": broken_links,
        "warnings": ([f"{len(broken_links)} broken relative Markdown links"] if broken_links else []),
        "errors": errors,
        "passed": len(errors) == 0,
    }
    report_path = snapshot.parent / "curate-report.json"
    write_json(report_path, report)
    record_stage_report({
        "run_id": run["run_id"],
        "stage": "curate",
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "status": "passed" if report["passed"] else "failed",
        "passed": report["passed"],
        "summary": {"file_count": report["file_count"], "unit_count": report["unit_count"]},
        "metrics": report,
        "warnings": report["warnings"],
        "errors": report["errors"],
        "schema_version": 1,
        "pipeline_version": "0.1.0",
    })
    if errors:
        raise RuntimeError(f"Curation failed for {len(errors)} files; see {report_path}")
    return report
