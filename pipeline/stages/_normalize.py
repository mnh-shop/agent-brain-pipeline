from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable

from charset_normalizer import from_bytes
from markdown_it import MarkdownIt
from markdown_it.token import Token

from pipeline.db import make_unit_id
from pipeline.schemas.ids import normalize_path
from pipeline.util import sha256_file, sha256_text

LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
    ".proto": "protobuf",
    ".graphql": "graphql",
}

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdown"}
TEXT_EXTENSIONS = {".txt", ".text", ".rst"}
SHELL_EXTENSIONS = {".sh", ".bash", ".zsh"}
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".rb", ".php", ".html", ".css", ".sql", ".proto", ".graphql",
}
SUPPORTED_TEXT_EXTENSIONS = MARKDOWN_EXTENSIONS | TEXT_EXTENSIONS | SHELL_EXTENSIONS | CODE_EXTENSIONS | {".json", ".yaml", ".yml", ".toml"}

MD = MarkdownIt("commonmark")


def sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def decode_file(path: Path, max_bytes: int) -> tuple[str | None, str | None, bool, str, bytes]:
    raw = path.read_bytes()
    try:
        import magic  # type: ignore

        mime = magic.from_buffer(raw[:8192], mime=True)
    except Exception:
        mime = None
    mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if b"\x00" in raw[:8192] or len(raw) > max_bytes:
        return None, None, True, mime, raw
    match = from_bytes(raw).best()
    if not match:
        return None, None, True, mime, raw
    text = str(match)
    if not text.strip() and raw:
        return None, str(match.encoding or "unknown"), True, mime, raw
    return text, str(match.encoding or "utf-8"), False, mime, raw


def file_line_ranges(raw: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for chunk in raw.splitlines(keepends=True):
        start = cursor
        cursor += len(chunk)
        ranges.append((start, cursor))
    if not raw:
        ranges.append((0, 0))
    return ranges


def slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "section"


def markdown_frontmatter(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    if not text.startswith("---\n"):
        return None, []
    lines = text.splitlines()
    if len(lines) < 3:
        return None, ["frontmatter is not closed"]
    closing = None
    for index, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        return None, ["frontmatter is not closed"]
    import yaml

    try:
        data = yaml.safe_load("\n".join(lines[1:closing - 1]))
    except Exception as exc:
        return None, [f"frontmatter parse failed: {exc}"]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, ["frontmatter must be a mapping"]
    return data, []


def markdown_headings(text: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    stack: list[str] = []
    for token in MD.parse(text):
        if token.type != "heading_open":
            continue
        inline = next((t for t in token.children or [] if t.type == "text"), None)
        title = inline.content.strip() if inline else ""
        level = int(token.tag.lstrip("h") or "1")
        while len(stack) >= level:
            stack.pop()
        stack.append(title)
        headings.append({
            "level": level,
            "title": title,
            "anchor": slugify(title),
            "path": " / ".join(filter(None, stack)),
        })
    return headings


def _line_offsets(text: str) -> list[tuple[int, int]]:
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for line in text.splitlines(keepends=True):
        start = cursor
        cursor += len(line.encode("utf-8"))
        offsets.append((start, cursor))
    if not offsets:
        offsets.append((0, 0))
    return offsets


def _unit_payload(
    *,
    source_id: str,
    platform: str,
    repository_url: str,
    namespace: str,
    repository_name: str,
    requested_ref: str | None,
    resolved_branch: str | None,
    commit_sha: str,
    path: str,
    unit_type: str,
    heading: str | None,
    language: str | None,
    start_line: int | None,
    end_line: int | None,
    start_byte: int | None,
    end_byte: int | None,
    file_sha256: str,
    content: str,
    parser_name: str,
    parser_version: str,
    pipeline_version: str,
) -> dict[str, Any]:
    normalized = normalize_path(path)
    content_sha256 = sha256_text(content)
    return {
        "unit_id": make_unit_id(
            source_id=source_id,
            commit_sha=commit_sha,
            path=normalized,
            unit_type=unit_type,
            start=start_line if start_line is not None else start_byte,
            end=end_line if end_line is not None else end_byte,
            content_hash=content_sha256,
        ),
        "source_id": source_id,
        "platform": platform,
        "repository_url": repository_url,
        "namespace": namespace,
        "repository_name": repository_name,
        "requested_ref": requested_ref,
        "resolved_branch": resolved_branch,
        "commit_sha": commit_sha,
        "path": path,
        "normalized_path": normalized,
        "unit_type": unit_type,
        "heading": heading,
        "language": language,
        "start_line": start_line,
        "end_line": end_line,
        "start_byte": start_byte,
        "end_byte": end_byte,
        "content_hash": content_sha256,
        "file_sha256": file_sha256,
        "content_sha256": content_sha256,
        "content": content,
        "parser_name": parser_name,
        "parser_version": parser_version,
        "schema_version": 1,
        "pipeline_version": pipeline_version,
        "provenance": {
            "source_id": source_id,
            "platform": platform,
            "repository_url": repository_url,
            "namespace": namespace,
            "repository_name": repository_name,
            "requested_ref": requested_ref,
            "resolved_branch": resolved_branch,
            "commit_sha": commit_sha,
            "path": path,
            "normalized_path": normalized,
            "file_sha256": file_sha256,
            "content_sha256": content_sha256,
            "source_line_start": start_line,
            "source_line_end": end_line,
            "source_byte_start": start_byte,
            "source_byte_end": end_byte,
            "parser_name": parser_name,
            "parser_version": parser_version,
            "schema_version": 1,
            "pipeline_version": pipeline_version,
        },
    }


def markdown_units(
    *,
    text: str,
    raw: bytes,
    path: str,
    source_id: str,
    platform: str,
    repository_url: str,
    namespace: str,
    repository_name: str,
    requested_ref: str | None,
    resolved_branch: str | None,
    commit_sha: str,
    file_sha256: str,
    pipeline_version: str,
    max_chars: int,
) -> list[dict[str, Any]]:
    lines = text.splitlines()
    offsets = file_line_ranges(raw)
    headings = markdown_headings(text)
    if not headings:
        headings = [{"level": 1, "title": Path(path).name, "path": Path(path).name}]
    heading_line_indices = [i for i, line in enumerate(lines, start=1) if re.match(r"^#{1,6}\s+", line)]
    if not heading_line_indices:
        heading_line_indices = [1]

    section_bounds: list[tuple[str, int, int]] = []
    sections: list[tuple[str, int, int]] = []
    for idx, start in enumerate(heading_line_indices):
        end = heading_line_indices[idx + 1] - 1 if idx + 1 < len(heading_line_indices) else len(lines)
        title = headings[min(idx, len(headings) - 1)]["path"]
        sections.append((title, start, end))

    units: list[dict[str, Any]] = []
    for title, section_start, section_end in sections:
        paragraph_start = None
        pending_lines: list[int] = []
        current_chars = 0
        for line_no in range(section_start, section_end + 1):
            line = lines[line_no - 1]
            if not line.strip():
                if pending_lines:
                    if paragraph_start is None:
                        paragraph_start = pending_lines[0]
                    units.extend(
                        _flush_markdown_chunk(
                            pending_lines, title, lines, offsets, path, source_id, platform, repository_url,
                            namespace, repository_name, requested_ref, resolved_branch, commit_sha,
                            file_sha256, pipeline_version,
                        )
                    )
                    pending_lines = []
                    current_chars = 0
                paragraph_start = None
                continue
            if paragraph_start is None:
                paragraph_start = line_no
            line_chars = len(line) + 1
            if pending_lines and current_chars + line_chars > max_chars:
                units.extend(
                    _flush_markdown_chunk(
                        pending_lines, title, lines, offsets, path, source_id, platform, repository_url,
                        namespace, repository_name, requested_ref, resolved_branch, commit_sha,
                        file_sha256, pipeline_version,
                    )
                )
                pending_lines = [line_no]
                current_chars = line_chars
            else:
                pending_lines.append(line_no)
                current_chars += line_chars
        if pending_lines:
            units.extend(
                _flush_markdown_chunk(
                    pending_lines, title, lines, offsets, path, source_id, platform, repository_url,
                    namespace, repository_name, requested_ref, resolved_branch, commit_sha,
                    file_sha256, pipeline_version,
                )
            )
    return units


def _flush_markdown_chunk(
    line_numbers: list[int],
    title: str,
    lines: list[str],
    offsets: list[tuple[int, int]],
    path: str,
    source_id: str,
    platform: str,
    repository_url: str,
    namespace: str,
    repository_name: str,
    requested_ref: str | None,
    resolved_branch: str | None,
    commit_sha: str,
    file_sha256: str,
    pipeline_version: str,
) -> list[dict[str, Any]]:
    if not line_numbers:
        return []
    start_line = line_numbers[0]
    end_line = line_numbers[-1]
    content = "\n".join(lines[start_line - 1:end_line])
    if not content.strip():
        return []
    start_byte = offsets[start_line - 1][0]
    end_byte = offsets[end_line - 1][1]
    return [
        _unit_payload(
            source_id=source_id,
            platform=platform,
            repository_url=repository_url,
            namespace=namespace,
            repository_name=repository_name,
            requested_ref=requested_ref,
            resolved_branch=resolved_branch,
            commit_sha=commit_sha,
            path=path,
            unit_type="markdown_section",
            heading=title,
            language="markdown",
            start_line=start_line,
            end_line=end_line,
            start_byte=start_byte,
            end_byte=end_byte,
            file_sha256=file_sha256,
            content=content,
            parser_name="markdown",
            parser_version="commonmark",
            pipeline_version=pipeline_version,
        )
    ]


def text_units(
    *,
    text: str,
    raw: bytes,
    path: str,
    source_id: str,
    platform: str,
    repository_url: str,
    namespace: str,
    repository_name: str,
    requested_ref: str | None,
    resolved_branch: str | None,
    commit_sha: str,
    file_sha256: str,
    language: str,
    parser_name: str,
    parser_version: str,
    pipeline_version: str,
    lines_per_chunk: int,
    overlap: int,
) -> list[dict[str, Any]]:
    lines = text.splitlines()
    offsets = file_line_ranges(raw)
    if not lines:
        return []
    units: list[dict[str, Any]] = []
    step = max(1, lines_per_chunk - overlap)
    for start_index in range(0, len(lines), step):
        end_index = min(len(lines), start_index + lines_per_chunk)
        chunk_lines = lines[start_index:end_index]
        content = "\n".join(chunk_lines)
        if not content.strip():
            continue
        start_line = start_index + 1
        end_line = end_index
        units.append(
            _unit_payload(
                source_id=source_id,
                platform=platform,
                repository_url=repository_url,
                namespace=namespace,
                repository_name=repository_name,
                requested_ref=requested_ref,
                resolved_branch=resolved_branch,
                commit_sha=commit_sha,
                path=path,
                unit_type="text_chunk",
                heading=None,
                language=language,
                start_line=start_line,
                end_line=end_line,
                start_byte=offsets[start_line - 1][0],
                end_byte=offsets[end_line - 1][1],
                file_sha256=file_sha256,
                content=content,
                parser_name=parser_name,
                parser_version=parser_version,
                pipeline_version=pipeline_version,
            )
        )
        if end_index >= len(lines):
            break
    return units


def code_units(*, text: str, raw: bytes, path: str, language: str, **kwargs: Any) -> list[dict[str, Any]]:
    return text_units(text=text, raw=raw, path=path, language=language, parser_name=language, parser_version="fallback", **kwargs)


def supported_language(path: Path) -> str:
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


def is_markdown(path: Path) -> bool:
    return path.suffix.lower() in MARKDOWN_EXTENSIONS


def is_text_like(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_TEXT_EXTENSIONS
