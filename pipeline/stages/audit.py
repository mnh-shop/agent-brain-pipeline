from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.db import PIPELINE_VERSION, record_stage_report, update_run
from pipeline.stages._audit import (
    build_artifact_manifest,
    compare_reproducibility,
    validate_graph_integrity,
    validate_markdown_exports,
    validate_retrieval_integrity,
    validate_source_integrity,
    validate_units_and_symbols,
    validate_vector_integrity,
)
from pipeline.util import sha256_text, write_json


def _serialise_checks(checks: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for item in checks:
        rows.append({"check": item.name, "passed": item.passed, **item.details})
    return rows


def _failed_checks(report: dict[str, Any]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for group, items in report.get("checks", {}).items():
        for item in items:
            if not item.get("passed"):
                failures.append({"group": group, **item})
    return failures


def _report(run: dict[str, Any]) -> dict[str, Any]:
    snapshot = Path(run["snapshot_path"])
    base = snapshot.parent
    source_checks = validate_source_integrity(run)
    unit_checks = validate_units_and_symbols(run)
    graph_checks = validate_graph_integrity(run)
    vector_checks = validate_vector_integrity(run)
    reproducibility = compare_reproducibility(run)
    artifact_manifest = build_artifact_manifest(run)
    markdown_checks = validate_markdown_exports(run, artifact_manifest)
    retrieval_checks = validate_retrieval_integrity(run)
    artifact_manifest_payload = json.dumps(artifact_manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    artifact_manifest_sha256 = sha256_text(artifact_manifest_payload)
    state = "ready_for_wiki" if all(item.passed for item in source_checks + unit_checks + graph_checks + vector_checks + markdown_checks + retrieval_checks) and reproducibility["passed"] else "failed"

    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "state": state,
        "checks": {
            "source": _serialise_checks(source_checks),
            "units": _serialise_checks(unit_checks),
            "graph": _serialise_checks(graph_checks),
            "vector": _serialise_checks(vector_checks),
            "markdown": _serialise_checks(markdown_checks),
            "retrieval": _serialise_checks(retrieval_checks),
            "reproducibility": [{"check": "reproducibility", "passed": reproducibility["passed"], **{k: v for k, v in reproducibility.items() if k != "passed"}}],
        },
        "artifact_manifest": artifact_manifest,
        "artifact_manifest_sha256": artifact_manifest_sha256,
        "reproducibility": reproducibility,
        "failed_checks": [],
    }
    report["passed"] = report["state"] == "ready_for_wiki"
    report["failed_checks"] = _failed_checks(report)
    return report


def run(run: dict[str, Any]) -> dict[str, Any]:
    snapshot = Path(run["snapshot_path"])
    base = snapshot.parent
    report = _report(run)
    report_path = base / "audit-report.json"
    reproducibility_path = base / "reproducibility-report.json"
    artifact_manifest_path = base / "artifact-manifest.json"

    write_json(report_path, report)
    write_json(reproducibility_path, report["reproducibility"])
    write_json(artifact_manifest_path, report["artifact_manifest"])
    artifact_manifest_sha = sha256_text(json.dumps(report["artifact_manifest"], sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    (base / "artifact-manifest.sha256").write_text(f"{artifact_manifest_sha}  artifact-manifest.json\n", encoding="utf-8")
    (base / "audit-report.md").write_text(
        "\n".join(
            [
                "# Audit report",
                "",
                f"- State: {report['state']}",
                f"- Passed: {report['passed']}",
                f"- Failed checks: {len(report['failed_checks'])}",
                f"- Checks: {sum(len(v) for v in report['checks'].values())}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (base / "reproducibility-report.md").write_text(
        "\n".join(
            [
                "# Reproducibility report",
                "",
                f"- Passed: {reproducibility['passed']}",
                f"- Current SHA256: {reproducibility['current_sha256']}",
                f"- Replay SHA256: {reproducibility['replay_sha256']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    update_run(
        run["run_id"],
        status="deterministic_passed" if report["passed"] else "failed",
        current_stage="audit",
        wiki_state="awaiting_wiki_agent" if report["passed"] else "wiki_validation_failed",
        error=None if report["passed"] else "quality gate failed",
        wiki_error=None if report["passed"] else "quality gate failed",
    )
    record_stage_report({
        "run_id": run["run_id"],
        "stage": "audit",
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "status": "passed" if report["passed"] else "failed",
        "passed": report["passed"],
        "summary": {"state": report["state"], "failed_checks": len(report["failed_checks"])},
        "metrics": report,
        "warnings": [] if report["passed"] else ["Deterministic gate failed"],
        "errors": [] if report["passed"] else report["failed_checks"],
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
    })
    if not report["passed"]:
        raise RuntimeError(f"Audit failed; see {report_path}")
    return report


def verify_run(run: dict[str, Any]) -> dict[str, Any]:
    return _report(run)
