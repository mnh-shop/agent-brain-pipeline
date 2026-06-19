from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any


def stable_hash(parts: list[Any]) -> str:
    data = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    parts = [part for part in PurePosixPath(normalized).parts if part not in {"."}]
    joined = "/".join(parts)
    return joined.removeprefix("./")


def unit_identity(
    *,
    source_id: str,
    commit_sha: str,
    path: str,
    unit_type: str,
    start: int | None,
    end: int | None,
    content_hash: str,
) -> str:
    return stable_hash([source_id, commit_sha, path, unit_type, start, end, content_hash])


def symbol_identity(
    *,
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
    return stable_hash([source_id, commit_sha, path, language, symbol_kind, qualified_name, start_byte, end_byte, content_hash])


def snapshot_identity(
    *,
    source_id: str,
    commit_sha: str,
    snapshot_path: str,
) -> str:
    return stable_hash([source_id, commit_sha, snapshot_path])
