from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any

import yaml

from pipeline.config import get_config
from pipeline.util import read_json, run_command, write_json
from pipeline.stages._normalize import markdown_frontmatter, markdown_headings, slugify


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _severity(message: str, level: str) -> dict[str, str]:
    return {"severity": level, "message": message}


def _finding(severity: str, check: str, path: str | None, message: str, **extra: Any) -> dict[str, Any]:
    payload = {"severity": severity, "check": check, "path": path, "message": message}
    payload.update(extra)
    return payload


def _lint_markdown(path: Path, text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    frontmatter, frontmatter_errors = markdown_frontmatter(text)
    for item in frontmatter_errors:
        findings.append(_finding("error", "markdown_frontmatter", path.as_posix(), item))
    headings = markdown_headings(text)
    anchors = [item["anchor"] for item in headings]
    seen: set[str] = set()
    for anchor in anchors:
        if anchor in seen:
            findings.append(_finding("warning", "duplicate_anchor", path.as_posix(), f"duplicate anchor `{anchor}`"))
        seen.add(anchor)
    titles: set[str] = set()
    for item in headings:
        title = item["title"]
        if title in titles:
            findings.append(_finding("warning", "duplicate_heading", path.as_posix(), f"duplicate heading `{title}`"))
        titles.add(title)
    for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
        raw = target.strip().split()[0].strip("<>")
        if not raw:
            continue
        if raw.startswith(("http://", "https://", "mailto:", "data:")):
            continue
        local = raw.split("#", 1)[0].split("?", 1)[0]
        if not local:
            continue
        candidate = (path.parent / local).resolve()
        if not candidate.exists():
            findings.append(_finding("error", "broken_markdown_link", path.as_posix(), f"broken local link `{raw}`"))
    if "\ufffd" in text:
        findings.append(_finding("warning", "unicode_replacement", path.as_posix(), "contains U+FFFD replacement characters"))
    if frontmatter is not None and not isinstance(frontmatter, dict):
        findings.append(_finding("error", "frontmatter_structure", path.as_posix(), "frontmatter must be a mapping"))
    return findings


def _lint_json(path: Path, text: str) -> list[dict[str, Any]]:
    try:
        json.loads(text)
        return []
    except Exception as exc:
        return [_finding("error", "json_parse", path.as_posix(), str(exc))]


def _lint_yaml(path: Path, text: str) -> list[dict[str, Any]]:
    try:
        yaml.safe_load(text)
        return []
    except Exception as exc:
        return [_finding("error", "yaml_parse", path.as_posix(), str(exc))]


def _lint_toml(path: Path, text: str) -> list[dict[str, Any]]:
    try:
        tomllib.loads(text)
        return []
    except Exception as exc:
        return [_finding("error", "toml_parse", path.as_posix(), str(exc))]


def _lint_python(path: Path, text: str, snapshot: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    try:
        compile(text, path.as_posix(), "exec")
    except Exception as exc:
        findings.append(_finding("error", "python_compile", path.as_posix(), str(exc)))
    if shutil.which("ruff"):
        result = run_command(["ruff", "check", "--output-format", "json", path.as_posix()], cwd=snapshot, check=False, timeout=120)
        if result.stdout.strip():
            try:
                diagnostics = json.loads(result.stdout)
            except Exception:
                diagnostics = []
            for diag in diagnostics:
                findings.append(_finding(
                    "error",
                    "ruff",
                    path.as_posix(),
                    diag.get("message", "ruff diagnostic"),
                    code=diag.get("code"),
                    location=diag.get("location"),
                ))
    return findings


def _lint_shell(path: Path, snapshot: Path) -> list[dict[str, Any]]:
    if not shutil.which("shellcheck"):
        return [_finding("info", "shellcheck", path.as_posix(), "shellcheck unavailable; skipped")]
    result = run_command(["shellcheck", "--format=json1", path.as_posix()], cwd=snapshot, check=False, timeout=120)
    if result.returncode == 0:
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception:
        payload = {}
    findings: list[dict[str, Any]] = []
    for item in payload.get("comments", []):
        findings.append(_finding("warning", "shellcheck", path.as_posix(), item.get("message", "shellcheck issue"), code=item.get("code")))
    if not findings:
        findings.append(_finding("warning", "shellcheck", path.as_posix(), result.stderr[-2000:] or "shellcheck reported issues"))
    return findings


def _lint_file(path: Path, text: str, encoding: str | None, snapshot: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    suffix = path.suffix.lower()
    if encoding and encoding.lower() not in {"utf-8", "utf8", "us-ascii", "ascii"}:
        findings.append(_finding("warning", "mixed_encoding", path.as_posix(), f"decoded as {encoding}"))
    if suffix == ".json":
        findings.extend(_lint_json(path, text))
    elif suffix in {".yaml", ".yml"}:
        findings.extend(_lint_yaml(path, text))
    elif suffix == ".toml":
        findings.extend(_lint_toml(path, text))
    elif suffix == ".py":
        findings.extend(_lint_python(path, text, snapshot))
    elif suffix in {".sh", ".bash", ".zsh"}:
        findings.extend(_lint_shell(path, snapshot))
    elif suffix in {".md", ".markdown", ".mdown"}:
        findings.extend(_lint_markdown(path, text))
    return findings


def _config() -> dict[str, Any]:
    return get_config().get("lint", {"enabled": True, "fail_severities": ["error", "fatal"], "check_external_links": False})


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    lint_cfg = _config()
    snapshot = Path(run["snapshot_path"])
    normalization_report = read_json(snapshot.parent / "normalization-report.json")
    files = _load_jsonl(Path(normalization_report["files_jsonl"]))
    units = _load_jsonl(Path(normalization_report["units_jsonl"]))

    findings: list[dict[str, Any]] = []
    for file_row in files:
        path = snapshot / file_row["path"]
        if not path.exists():
            findings.append(_finding("error", "missing_normalized_file", file_row["path"], "file missing from snapshot"))
            continue
        if file_row.get("is_binary"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        findings.extend(_lint_file(path, text, file_row.get("encoding"), snapshot))

    duplicate_units: dict[str, list[str]] = {}
    for unit in units:
        duplicate_units.setdefault(unit["content_sha256"], []).append(unit["path"])
    for content_hash, paths in duplicate_units.items():
        if len(paths) > 1:
            findings.append(_finding("warning", "duplicate_content", None, f"duplicate content hash {content_hash}", paths=paths))

    fail_severities = {str(sev).lower() for sev in lint_cfg.get("fail_severities", ["error", "fatal"])}
    passed = not any(item["severity"].lower() in fail_severities for item in findings)
    severity_counts = {level: sum(1 for item in findings if item["severity"] == level) for level in ("info", "warning", "error", "fatal")}
    report = {
        "schema_version": 1,
        "pipeline_version": "0.1.0",
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "snapshot_path": str(snapshot),
        "fail_severities": sorted(fail_severities),
        "findings": findings,
        "severity_counts": severity_counts,
        "passed": passed,
    }
    report_path = snapshot.parent / "lint-report.json"
    write_json(report_path, report)
    (snapshot.parent / "lint-report.md").write_text(
        "\n".join([
            "# Lint Report",
            "",
            f"- Run: `{run['run_id']}`",
            f"- Commit: `{run['commit_sha']}`",
            f"- Passed: `{passed}`",
            f"- Findings: {len(findings)}",
        ]) + "\n",
        encoding="utf-8",
    )

    integrity = read_json(snapshot.parent / "integrity-report.json") if (snapshot.parent / "integrity-report.json").exists() else {"passed": False}
    normalization = normalization_report
    compatibility = {
        "schema_version": 1,
        "pipeline_version": "0.1.0",
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "passed": bool(integrity.get("passed")) and bool(normalization.get("passed")) and passed,
        "integrity": integrity,
        "normalization": normalization,
        "lint": report,
    }
    write_json(snapshot.parent / "curate-report.json", compatibility)
    return report
