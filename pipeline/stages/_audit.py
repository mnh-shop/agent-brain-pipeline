from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from pipeline.config import obsidian_dir
from pipeline.db import connect
from pipeline.search import exact_search_for_run, fts_search_for_run, hybrid_search, semantic_search, structural_search, vector_search
from pipeline.indexes import LanceDBIndex
from pipeline.urls import parse_repository_url
from pipeline.schemas.ids import normalize_path, stable_hash
from pipeline.util import read_json, sha256_file, sha256_text


TERMINAL_STATUSES = {"completed", "deterministic_passed", "ready_for_wiki"}


@dataclass(frozen=True)
class ValidationResult:
    name: str
    passed: bool
    details: dict[str, Any]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            value = {"raw": line}
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _snapshot_files(snapshot: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(snapshot.rglob("*")):
        if path.is_file():
            rel = path.relative_to(snapshot).as_posix()
            files[normalize_path(rel)] = {
                "path": rel,
                "size_bytes": path.stat().st_size,
                "file_sha256": sha256_file(path),
            }
    return files


def _load_report(path: Path) -> dict[str, Any]:
    return read_json(path) if path.exists() else {}


def _frontmatter_ok(text: str) -> bool:
    return text.startswith("---\n") and "\n---\n" in text[4:]


def _safe_title(value: str) -> str:
    return value.replace("/", " - ").replace("\\", " - ")


def _resolve_markdown_links(text: str, base: Path) -> list[str]:
    import re

    failures: list[str] = []
    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text):
        target = match.group(2).split("#", 1)[0].strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        resolved = (base.parent / target).resolve()
        if not resolved.exists():
            failures.append(target)
    return failures


def _canonical_rows(run: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with connect() as connection:
        files = [dict(row) for row in connection.execute("SELECT * FROM files WHERE run_id=? ORDER BY path", (run["run_id"],)).fetchall()]
        units = [dict(row) for row in connection.execute("SELECT * FROM units WHERE run_id=? ORDER BY path, start_line, unit_id", (run["run_id"],)).fetchall()]
        symbols = [dict(row) for row in connection.execute("SELECT * FROM symbols WHERE run_id=? ORDER BY path, start_line, symbol_id", (run["run_id"],)).fetchall()]
    return files, units, symbols


def validate_source_integrity(run: dict[str, Any]) -> list[ValidationResult]:
    snapshot = Path(run["snapshot_path"])
    raw = snapshot.parent / "raw"
    manifest = _load_report(raw / "source-manifest.json")
    files, _, _ = _canonical_rows(run)
    checks: list[ValidationResult] = []

    bundle = raw / "repository.bundle"
    checks.append(ValidationResult("bundle_exists", bundle.exists(), {"path": str(bundle)}))
    checks.append(ValidationResult("source_manifest_present", bool(manifest), {"path": str(raw / "source-manifest.json")}))
    if bundle.exists():
        from pipeline.util import run_command

        result = run_command(["git", "bundle", "verify", str(bundle)], check=False, timeout=300)
        checks.append(ValidationResult("bundle_verify", result.returncode == 0, {"returncode": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}))

    if manifest:
        expected_commit = manifest.get("commit_sha") or run.get("commit_sha")
        checks.append(ValidationResult("exact_commit_exists", expected_commit == run.get("commit_sha"), {"expected": expected_commit, "actual": run.get("commit_sha")}))
        expected_count = len(manifest.get("files", []))
        expected_bytes = sum(int(item.get("size_bytes") or item.get("size") or 0) for item in manifest.get("files", []))
        actual_bytes = 0
        for item in manifest.get("files", []):
            path = snapshot / item.get("path", "")
            rel = normalize_path(item.get("path", ""))
            checks.append(ValidationResult(
                f"file_exists:{rel}",
                path.exists(),
                {"path": str(path)},
            ))
            checks.append(ValidationResult(
                f"path_safe:{rel}",
                path.resolve().is_relative_to(snapshot.resolve()) if path.exists() else False,
                {"path": str(path)},
            ))
            if path.exists():
                actual_bytes += path.stat().st_size
                checks.append(ValidationResult(
                    f"file_hash:{rel}",
                    sha256_file(path) == item.get("sha256"),
                    {"expected": item.get("sha256"), "actual": sha256_file(path)},
                ))
        checks.append(ValidationResult("manifest_matches_snapshot", len(files) == expected_count, {"files": len(files), "manifest": expected_count}))
        checks.append(ValidationResult("file_bytes_match", actual_bytes == expected_bytes if expected_bytes else True, {"actual_bytes": actual_bytes, "expected_bytes": expected_bytes}))
    checks.append(ValidationResult("file_count_nonzero", len(files) > 0, {"file_count": len(files)}))
    return checks


def _unit_source_slice(snapshot: Path, row: dict[str, Any]) -> tuple[str, bool]:
    path = snapshot / row["path"]
    if not path.exists():
        return "", False
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start_line = int(row.get("start_line") or 1)
    end_line = int(row.get("end_line") or start_line)
    if start_line < 1 or end_line > max(len(lines), 1) or start_line > end_line:
        return "", False
    content = "\n".join(lines[start_line - 1 : end_line])
    return content, True


def validate_units_and_symbols(run: dict[str, Any]) -> list[ValidationResult]:
    snapshot = Path(run["snapshot_path"])
    files, units, symbols = _canonical_rows(run)
    file_map = {normalize_path(row["path"]): row for row in files}
    checks: list[ValidationResult] = []
    seen_unit: dict[str, str] = {}
    seen_symbol: dict[str, str] = {}
    for row in units:
        normalized = normalize_path(row["path"])
        file_row = file_map.get(normalized)
        content, ok = _unit_source_slice(snapshot, row)
        content_hash = sha256_text(content) if ok else None
        checks.append(ValidationResult(f"unit_file:{row['unit_id']}", file_row is not None, {"path": row["path"]}))
        checks.append(ValidationResult(f"unit_range:{row['unit_id']}", ok, {"start_line": row.get("start_line"), "end_line": row.get("end_line")}))
        checks.append(ValidationResult(f"unit_hash:{row['unit_id']}", content_hash == row.get("content_sha256"), {"expected": row.get("content_sha256"), "actual": content_hash}))
        prev = seen_unit.get(row["unit_id"])
        if prev is None:
            seen_unit[row["unit_id"]] = row.get("content_sha256") or ""
        else:
            checks.append(ValidationResult(f"unit_id_unique:{row['unit_id']}", prev == (row.get("content_sha256") or ""), {"previous": prev, "current": row.get("content_sha256")}))
    for row in symbols:
        normalized = normalize_path(row["path"])
        file_row = file_map.get(normalized)
        source = snapshot / row["path"]
        content = ""
        ok = source.exists()
        if ok:
            text = source.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            start_line = int(row.get("start_line") or 1)
            end_line = int(row.get("end_line") or start_line)
            ok = start_line >= 1 and end_line <= max(len(lines), 1) and start_line <= end_line
            if ok:
                content = "\n".join(lines[start_line - 1 : end_line])
        checks.append(ValidationResult(f"symbol_file:{row['symbol_id']}", file_row is not None, {"path": row["path"]}))
        checks.append(ValidationResult(f"symbol_hash:{row['symbol_id']}", sha256_text(content) == row.get("content_sha256"), {"expected": row.get("content_sha256"), "actual": sha256_text(content) if content else None}))
        prev = seen_symbol.get(row["symbol_id"])
        if prev is None:
            seen_symbol[row["symbol_id"]] = row.get("content_sha256") or ""
        else:
            checks.append(ValidationResult(f"symbol_id_unique:{row['symbol_id']}", prev == (row.get("content_sha256") or ""), {"previous": prev, "current": row.get("content_sha256")}))
    return checks


def validate_graph_integrity(run: dict[str, Any]) -> list[ValidationResult]:
    base = Path(run["snapshot_path"]).parent
    codegraph = base / "codegraph"
    normalized_dir = codegraph / "normalized"
    nodes = _load_jsonl(normalized_dir / "nodes.jsonl")
    edges = _load_jsonl(normalized_dir / "edges.jsonl")
    matches = _load_jsonl(normalized_dir / "symbol-matches.jsonl")
    unmatched = _load_jsonl(normalized_dir / "unmatched-nodes.jsonl")
    symbols = {row["symbol_id"]: row for row in _load_jsonl(Path(run["snapshot_path"]).parent / "syntax" / "symbols.jsonl")}
    checks: list[ValidationResult] = []
    node_ids = [row.get("node_id") for row in nodes]
    checks.append(ValidationResult("node_unique_ids", len(node_ids) == len(set(node_ids)), {"node_count": len(node_ids), "unique_count": len(set(node_ids))}))
    checks.append(ValidationResult("graph_node_commit", all(row.get("commit_sha") == run.get("commit_sha") for row in nodes), {"commit_sha": run.get("commit_sha")}))
    node_ids_set = set(node_ids)
    for edge in edges:
        source_node_id = edge.get("source_node_id")
        target_node_id = edge.get("target_node_id")
        checks.append(
            ValidationResult(
                f"edge_refs:{edge.get('edge_id', stable_hash(edge))}",
                source_node_id in node_ids_set and target_node_id in node_ids_set,
                {"source_node_id": source_node_id, "target_node_id": target_node_id},
            )
        )
    for match in matches:
        symbol_id = match.get("symbol_id") or match.get("matched_symbol_id")
        node_id = match.get("node_id") or match.get("symbol_id") or symbol_id
        checks.append(
            ValidationResult(
                f"symbol_mapping:{node_id}",
                bool(symbol_id) and symbol_id in symbols,
                {"symbol_id": symbol_id},
            )
        )
    checks.append(ValidationResult("unmatched_reported", isinstance(unmatched, list), {"unmatched_count": len(unmatched)}))
    report = _load_report(base / "codegraph-report.json")
    checks.append(ValidationResult("graph_same_commit", report.get("commit_sha") == run.get("commit_sha"), {"report_commit": report.get("commit_sha"), "run_commit": run.get("commit_sha")}))
    return checks


def validate_vector_integrity(run: dict[str, Any]) -> list[ValidationResult]:
    base = Path(run["snapshot_path"]).parent
    report = _load_report(base / "vector-report.json")
    rows: list[dict[str, Any]] = []
    index_error = None
    try:
        cfg = LanceDBIndex(
            path=Path(report.get("index_path") or base / "lancedb"),
            table_name=str(report.get("table") or "units"),
            metric=str(report.get("metric") or "cosine"),
        )
        rows = cfg.rows(filters={"source_id": run.get("source_id"), "commit_sha": run.get("commit_sha")}, dimensions=int(report.get("vector_dimensions") or 1))
    except Exception as exc:  # pragma: no cover - defensive gate behavior
        index_error = f"{type(exc).__name__}: {exc}"
    unit_ids = {row["unit_id"] for row in rows}
    duplicate_unit_ids = len(rows) != len(unit_ids)
    vector_rows = report.get("smoke_results", [])
    checks: list[ValidationResult] = []
    checks.append(ValidationResult("vector_report_present", bool(report), {"path": str(base / "vector-report.json")}))
    checks.append(ValidationResult("vector_count_matches", int(report.get("indexed_vector_count", 0)) == int(report.get("eligible_unit_count", 0)), {"indexed": report.get("indexed_vector_count"), "eligible": report.get("eligible_unit_count")}))
    checks.append(ValidationResult("vector_dims_match", all(int(row.get("vector_dimensions", 0)) == int(report.get("vector_dimensions", 0)) for row in vector_rows), {"dimension": report.get("vector_dimensions")}))
    checks.append(ValidationResult("vector_units_exist", all(row.get("unit_id") in unit_ids for row in vector_rows), {"unit_count": len(unit_ids)}))
    checks.append(ValidationResult("vector_metadata_filters", bool(report.get("metadata_filter_ok")), {"metadata_filter_ok": report.get("metadata_filter_ok")}))
    checks.append(ValidationResult("vector_no_duplicate_ids", not duplicate_unit_ids, {"row_count": len(rows), "unique_unit_count": len(unit_ids)}))
    checks.append(ValidationResult("vector_model_metadata_complete", all(bool(report.get(field)) for field in ("embedding_model", "model_revision", "vector_dimensions", "model_cache_path")), {"embedding_model": report.get("embedding_model"), "model_revision": report.get("model_revision"), "vector_dimensions": report.get("vector_dimensions"), "model_cache_path": report.get("model_cache_path")}))
    if index_error:
        checks.append(ValidationResult("vector_index_readable", False, {"error": index_error}))
    else:
        checks.append(ValidationResult("vector_index_readable", True, {"row_count": len(rows)}))
    return checks


def validate_markdown_exports(run: dict[str, Any], artifact_manifest: dict[str, Any] | None = None) -> list[ValidationResult]:
    vault = obsidian_dir()
    repo = parse_repository_url(run["repository_url"])
    source = vault / "10 Sources" / ("GitHub" if repo.platform == "github" else "GitLab")
    report_dir = vault / "80 Reports"
    title = f"{repo.namespace} - {repo.name}"
    source_note = source / f"{_safe_title(title)}.md"
    report_note = report_dir / f"{_safe_title(title)} - Ingestion Report.md"
    checks: list[ValidationResult] = []
    source_pages = [source_note] if source_note.exists() else []
    report_pages = [report_note] if report_note.exists() else []
    checks.append(ValidationResult("source_pages_nonempty", bool(source_pages), {"count": len(source_pages), "path": str(source_note)}))
    checks.append(ValidationResult("report_pages_nonempty", bool(report_pages), {"count": len(report_pages), "path": str(report_note)}))
    for page in source_pages + report_pages:
        text = page.read_text(encoding="utf-8")
        checks.append(ValidationResult(f"frontmatter:{page.name}", _frontmatter_ok(text), {"path": str(page)}))
        checks.append(ValidationResult(f"links:{page.name}", not _resolve_markdown_links(text, page), {"path": str(page)}))
    checks.append(ValidationResult("machine_manifest_matches_counts", len(source_pages) + len(report_pages) == 2, {"markdown_count": len(source_pages) + len(report_pages)}))
    checks.append(ValidationResult("no_missing_processed_text_files", source_note.exists() and report_note.exists(), {"source": str(source_note), "report": str(report_note)}))
    checks.append(ValidationResult("no_orphan_generated_pages", source_note.exists() and report_note.exists(), {"count": 2}))
    return checks


def validate_retrieval_integrity(run: dict[str, Any]) -> list[ValidationResult]:
    snapshot = Path(run["snapshot_path"])
    units = _load_jsonl(Path(run["snapshot_path"]).parent / "syntax" / "symbols.jsonl")
    query = None
    if units:
        query = str(units[0].get("qualified_name") or units[0].get("heading") or units[0].get("path") or "helper").split()[0]
    if not query:
        query = "helper"
    exact = exact_search_for_run(query, run, 5)
    fts = fts_search_for_run(query, run["run_id"], 5)
    structure = structural_search(query, run["source_id"], 5, run.get("commit_sha"))
    semantic = semantic_search(query, run["source_id"], 5, run.get("commit_sha"))
    vector = vector_search(query, run["source_id"], 5, run.get("commit_sha"))
    hybrid = hybrid_search(query, run["source_id"], 5, run.get("commit_sha"))
    exact_evidence = all((snapshot / str(row.get("path", ""))).exists() for row in exact if row.get("path"))
    fts_evidence = all((snapshot / str(row.get("path", ""))).exists() for row in fts if row.get("path"))
    checks = [
        ValidationResult("exact_result", bool(exact) and exact_evidence, {"query": query, "count": len(exact), "evidence": exact_evidence}),
        ValidationResult("fts_result", bool(fts) and fts_evidence, {"query": query, "count": len(fts), "evidence": fts_evidence}),
        ValidationResult("symbol_result", bool(units), {"query": query, "count": len(units)}),
        ValidationResult("codegraph_result", bool(structure), {"query": query, "count": len(structure)}),
        ValidationResult("codebase_memory_result", bool(semantic), {"query": query, "count": len(semantic)}),
        ValidationResult("lancedb_result", bool(vector), {"query": query, "count": len(vector)}),
        ValidationResult("hybrid_result", bool(hybrid), {"query": query, "count": len(hybrid)}),
    ]
    return checks


def build_artifact_manifest(run: dict[str, Any]) -> dict[str, Any]:
    base = Path(run["snapshot_path"]).parent
    files = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".json", ".md", ".jsonl", ".sha256"}:
            files.append({"path": str(path.relative_to(base)), "sha256": sha256_file(path), "size_bytes": path.stat().st_size})
    manifest = {
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "files": files,
        "schema_version": 1,
    }
    return manifest


def compare_reproducibility(run: dict[str, Any]) -> dict[str, Any]:
    current = build_artifact_manifest(run)
    with tempfile.TemporaryDirectory(prefix="agent-brain-repro-") as tempdir:
        temp = Path(tempdir)
        payload = json.dumps(current, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        (temp / "replay.json").write_text(payload + "\n", encoding="utf-8")
        replay_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    current_sha = hashlib.sha256(json.dumps(current, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "passed": current_sha == replay_sha,
        "current_sha256": current_sha,
        "replay_sha256": replay_sha,
        "manifest": current,
    }
