from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.db import connect
from pipeline.search import exact_search_for_run, fts_search_for_run
from pipeline.util import read_json, sha256_file, write_json


def run(run: dict[str, Any]) -> dict[str, Any]:
    snapshot = Path(run["snapshot_path"])
    raw = snapshot.parent / "raw"
    required_files = [
        raw / "repository.bundle",
        raw / "mirror.git.tar.zst",
        raw / "source.tar.zst",
        raw / "source-manifest.json",
        raw / "checksums.sha256",
        snapshot.parent / "integrity-report.json",
        snapshot.parent / "normalization-report.json",
        snapshot.parent / "lint-report.json",
        snapshot.parent / "curate-report.json",
        snapshot.parent / "structure-report.json",
        snapshot.parent / "semantic-report.json",
        snapshot.parent / "retrieval-report.json",
    ]
    checks = []
    for path in required_files:
        checks.append({"check": f"exists:{path.name}", "passed": path.exists(), "path": str(path)})

    if (raw / "source-manifest.json").exists():
        manifest = read_json(raw / "source-manifest.json")
        for name, expected in manifest.get("checksums", {}).items():
            target = raw / name
            actual = sha256_file(target) if target.exists() else None
            checks.append({"check": f"sha256:{name}", "passed": actual == expected, "expected": expected, "actual": actual})

    report_files = {
        "integrity": "integrity-report.json",
        "normalize": "normalization-report.json",
        "lint": "lint-report.json",
        "structure": "structure-report.json",
        "semantic": "semantic-report.json",
        "retrieval": "retrieval-report.json",
    }
    for name, filename in report_files.items():
        path = snapshot.parent / filename
        value = read_json(path) if path.exists() else {}
        checks.append({"check": f"report_passed:{name}", "passed": bool(value.get("passed")), "path": str(path)})

    with connect() as connection:
        file_count = connection.execute("SELECT count(*) FROM files WHERE run_id=?", (run["run_id"],)).fetchone()[0]
        unit_count = connection.execute("SELECT count(*) FROM units WHERE run_id=?", (run["run_id"],)).fetchone()[0]
    checks.append({"check": "file_catalog_nonempty", "passed": file_count > 0, "count": file_count})
    checks.append({"check": "search_units_nonempty", "passed": unit_count > 0, "count": unit_count})

    sample = None
    with connect() as connection:
        sample = connection.execute("SELECT content,path FROM units WHERE run_id=? ORDER BY path LIMIT 1", (run["run_id"],)).fetchone()
    if sample:
        token = next((word for word in sample["content"].split() if len(word) >= 4), None)
        if token:
            token = token.strip("`*_#[](){}<>,.;:'\"")
            fts_ok = bool(fts_search_for_run(token, run["run_id"], 3)) if token else False
            exact_ok = bool(exact_search_for_run(token, run, 3)) if token else False
            checks.append({"check": "fts_smoke", "passed": fts_ok, "query": token})
            checks.append({"check": "exact_smoke", "passed": exact_ok, "query": token})

    passed = all(item["passed"] for item in checks)
    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "checks": checks,
        "passed": passed,
    }
    report_path = snapshot.parent / "audit-report.json"
    write_json(report_path, report)
    if not passed:
        failures = [item["check"] for item in checks if not item["passed"]]
        raise RuntimeError(f"Audit failed: {', '.join(failures)}; see {report_path}")
    return report
