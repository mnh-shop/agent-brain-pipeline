from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.db import PIPELINE_VERSION, connect, record_stage_report
from pipeline.embeddings import LocalDeterministicEmbeddingBackend
from pipeline.indexes import LanceDBIndex
from pipeline.schemas.ids import normalize_path
from pipeline.util import write_json


SUPPORTED_UNIT_TYPES = {
    "markdown_section",
    "markdown_paragraph",
    "module",
    "class",
    "function",
    "method",
    "constructor",
    "interface",
    "enum",
    "configuration-object",
    "constant",
    "code_chunk",
}


def _vector_checksum(vector: list[float]) -> str:
    payload = ",".join(f"{value:.12f}" for value in vector)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_backend(cfg: dict[str, Any]) -> LocalDeterministicEmbeddingBackend:
    embeddings = cfg.get("embeddings", {})
    return LocalDeterministicEmbeddingBackend(
        model=str(embeddings.get("model", "agent-brain-local-hash-embedding")),
        revision=str(embeddings.get("revision", "v1")),
        dimensions=int(embeddings.get("dimensions", 384)),
        normalize=bool(embeddings.get("normalize", True)),
    )


def _eligible_units(run: dict[str, Any]) -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT u.*, r.status AS run_status, r.completed_at, r.updated_at AS run_updated_at
            FROM units u
            JOIN runs r ON r.run_id = u.run_id
            WHERE r.status IN ('running', 'completed')
            ORDER BY COALESCE(r.completed_at, r.updated_at), u.path, u.start_line, u.unit_id
            """,
        ).fetchall()
    deduped: dict[str, dict[str, Any]] = {}
    raw_count = 0
    for row in rows:
        data = dict(row)
        if data.get("unit_type") not in SUPPORTED_UNIT_TYPES:
            continue
        raw_count += 1
        previous = deduped.get(data["unit_id"])
        if not previous:
            deduped[data["unit_id"]] = data
            continue
        previous_key = (previous.get("completed_at") or previous.get("run_updated_at") or "", previous.get("run_id") or "")
        current_key = (data.get("completed_at") or data.get("run_updated_at") or "", data.get("run_id") or "")
        if current_key >= previous_key:
            deduped[data["unit_id"]] = data
    return {
        "rows": list(deduped.values()),
        "raw_count": raw_count,
        "duplicate_count": max(0, raw_count - len(deduped)),
    }


def _symbol_index(run: dict[str, Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT symbol_id, source_id, commit_sha, path, normalized_path, language, symbol_kind,
                   qualified_name, start_byte, end_byte, start_line, end_line, content_sha256
            FROM symbols
            WHERE run_id=?
            """,
            (run["run_id"],),
        ).fetchall()
    index: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        key = (
            normalize_path(data.get("normalized_path") or data.get("path") or ""),
            data.get("commit_sha"),
            data.get("symbol_kind"),
            data.get("qualified_name"),
            data.get("start_line"),
            data.get("end_line"),
            data.get("content_sha256"),
        )
        index[key] = data
    return index


def _unit_metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("metadata_json")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    metadata = row.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _match_symbol(row: dict[str, Any], symbol_index: dict[tuple[Any, ...], dict[str, Any]]) -> dict[str, Any] | None:
    normalized = normalize_path(row.get("normalized_path") or row.get("path") or "")
    metadata = _unit_metadata(row)
    symbol_id = metadata.get("symbol_id")
    qualified_name = metadata.get("qualified_name") or row.get("heading")
    symbol_kind = metadata.get("kind") or row.get("unit_type")
    candidates = [
        (
            normalized,
            row.get("commit_sha"),
            symbol_kind,
            qualified_name,
            row.get("start_line"),
            row.get("end_line"),
            row.get("content_sha256"),
        ),
        (
            normalized,
            row.get("commit_sha"),
            symbol_kind,
            None,
            row.get("start_line"),
            row.get("end_line"),
            row.get("content_sha256"),
        ),
    ]
    if symbol_id:
        for symbol in symbol_index.values():
            if symbol.get("symbol_id") == symbol_id:
                return symbol
    for key in candidates:
        symbol = symbol_index.get(key)
        if symbol:
            return symbol
    return None


def _build_records(run: dict[str, Any], backend: LocalDeterministicEmbeddingBackend) -> dict[str, Any]:
    eligible = _eligible_units(run)
    units = eligible["rows"]
    symbol_index = _symbol_index(run)
    texts = []
    rows = []
    for row in units:
        normalized = normalize_path(row.get("normalized_path") or row.get("path") or "")
        text = "\n\n".join(part for part in (row.get("heading") or "", row.get("content") or "", normalized) if part)
        texts.append(text)
        rows.append(row)
    vectors = backend.embed(texts) if texts else []
    records = []
    for row, vector in zip(rows, vectors):
        symbol = _match_symbol(row, symbol_index)
        record = {
            "unit_id": row["unit_id"],
            "source_id": row["source_id"],
            "platform": row.get("platform") or "unknown",
            "repository_url": row.get("repository_url") or "",
            "commit_sha": row["commit_sha"],
            "path": row["path"],
            "normalized_path": row.get("normalized_path") or normalize_path(row["path"]),
            "unit_type": row["unit_type"],
            "symbol_id": symbol["symbol_id"] if symbol else None,
            "heading": row.get("heading"),
            "language": row.get("language"),
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "start_byte": row.get("start_byte"),
            "end_byte": row.get("end_byte"),
            "content": row.get("content"),
            "content_sha256": row.get("content_sha256"),
            "embedding_model": backend.info.model,
            "model_revision": backend.info.revision,
            "vector_dimensions": backend.info.dimensions,
            "vector_checksum": _vector_checksum(vector),
            "created_pipeline_version": PIPELINE_VERSION,
            "run_id": row["run_id"],
            "metadata": {
                "source_id": row["source_id"],
                "commit_sha": row["commit_sha"],
                "unit_type": row["unit_type"],
                "symbol_id": symbol["symbol_id"] if symbol else None,
            },
            "vector": vector,
        }
        records.append(record)
    return {
        "units": units,
        "records": records,
        "backend": backend.info,
        "raw_count": eligible["raw_count"],
        "duplicate_count": eligible["duplicate_count"],
    }


def run(run: dict[str, Any], *, backend: LocalDeterministicEmbeddingBackend | None = None) -> dict[str, Any]:
    cfg = get_config()
    section = cfg.get("lancedb", {})
    if not section.get("enabled", True):
        return {"passed": not section.get("required", True), "skipped": True}

    backend = backend or _load_backend(cfg)
    index = LanceDBIndex(
        path=Path(section.get("path", "/data/indexes/lancedb")),
        table_name=str(section.get("table", "units")),
        metric=str(section.get("metric", "cosine")),
    )
    built = _build_records(run, backend)
    index.rebuild(built["records"])
    smoke_query = None
    smoke_results: list[dict[str, Any]] = []
    if built["records"]:
        smoke_record = next((row for row in built["records"] if row.get("content")), built["records"][0])
        smoke_query = (smoke_record.get("heading") or smoke_record.get("content") or smoke_record.get("path") or "").split()[0:3]
        smoke_query = " ".join(smoke_query) if smoke_query else "repository"
        smoke_results = index.search(backend.embed([smoke_query])[0], limit=5, filters={"source_id": run["source_id"], "commit_sha": run["commit_sha"]})

    indexed_count = index.count()
    unique_unit_ids = {row["unit_id"] for row in built["records"]}
    duplicate_unit_ids = built["duplicate_count"]
    dimension_mismatches = sum(1 for row in built["records"] if int(row["vector_dimensions"]) != int(backend.info.dimensions))
    mapped_vectors = sum(1 for row in built["records"] if row.get("unit_id"))
    symbol_mapped_vectors = sum(1 for row in built["records"] if row.get("symbol_id"))
    filters_ok = bool(smoke_results) and all(row.get("source_id") == run["source_id"] and row.get("commit_sha") == run["commit_sha"] for row in smoke_results)
    smoke_ok = bool(smoke_results) and any(row.get("content") for row in smoke_results)
    model_artifacts = list(backend.info.artifact_hashes)
    cache_path = backend.info.cache_path

    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "index_path": str(index.path),
        "table": index.table_name,
        "backend": index.backend,
        "embedding_model": backend.info.model,
        "model_revision": backend.info.revision,
        "vector_dimensions": backend.info.dimensions,
        "normalize": backend.info.normalize,
        "model_artifact_hashes": model_artifacts,
        "model_cache_path": cache_path,
        "raw_unit_count": built["raw_count"],
        "eligible_unit_count": len(unique_unit_ids),
        "indexed_vector_count": indexed_count,
        "duplicate_unit_id_count": duplicate_unit_ids,
        "dimension_mismatch_count": dimension_mismatches,
        "mapped_vector_count": mapped_vectors,
        "symbol_mapped_vector_count": symbol_mapped_vectors,
        "metadata_filter_ok": filters_ok,
        "smoke_query": smoke_query,
        "smoke_result_count": len(smoke_results),
        "smoke_results": smoke_results,
        "passed": bool(
            built["records"]
            and indexed_count == len(unique_unit_ids)
            and dimension_mismatches == 0
            and duplicate_unit_ids >= 0
            and mapped_vectors == len(built["records"])
            and filters_ok
            and smoke_ok
        ),
        "pipeline_version": PIPELINE_VERSION,
    }

    report_path = Path(run["snapshot_path"]).parent / "vector-report.json"
    write_json(report_path, report)
    (Path(run["snapshot_path"]).parent / "vector-report.md").write_text(
        "\n".join(
            [
                "# Vector index report",
                "",
                f"- Backend: {index.backend}",
                f"- Model: {backend.info.model}",
                f"- Revision: {backend.info.revision or 'unknown'}",
                f"- Eligible vectors: {report['eligible_unit_count']}",
                f"- Indexed vectors: {report['indexed_vector_count']}",
                f"- Smoke query: {report['smoke_query'] or 'n/a'}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record_stage_report({
        "run_id": run["run_id"],
        "stage": "vector",
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "status": "passed" if report["passed"] else "failed",
        "passed": report["passed"],
        "summary": {"vector_count": report["indexed_vector_count"]},
        "metrics": report,
        "warnings": [] if report["passed"] else ["Vector indexing validation failed"],
        "errors": [] if report["passed"] else [{"stage": "vector", "report_path": str(report_path)}],
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
    })
    if not report["passed"] and section.get("required", True):
        raise RuntimeError(f"Vector stage failed; see {report_path}")
    return report
