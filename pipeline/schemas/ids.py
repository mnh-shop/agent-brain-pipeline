from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Any


def stable_hash(*parts: Any) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def normalize_path(path: str) -> str:
    normalized = str(PurePosixPath(path.replace("\\", "/")))
    if normalized in {".", ""}:
        return "."
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized.removeprefix("./")


def unit_identity(
    source_id: str,
    commit_sha: str,
    path: str,
    unit_type: str,
    start: int | None,
    end: int | None,
    content_hash: str,
) -> str:
    return stable_hash(source_id, commit_sha, normalize_path(path), unit_type, start, end, content_hash)


def symbol_identity(
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
    return stable_hash(
        source_id,
        commit_sha,
        normalize_path(path),
        language,
        symbol_kind,
        qualified_name,
        start_byte,
        end_byte,
        content_hash,
    )


def snapshot_identity(source_id: str, commit_sha: str, requested_ref: str | None = None) -> str:
    return stable_hash(source_id, commit_sha, requested_ref or "")
