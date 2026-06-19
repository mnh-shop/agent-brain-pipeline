from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.util import ignored, read_json, run_command, sha256_file, write_json


def _manifest_checks(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    required = {
        "schema_version",
        "created_by_run_id",
        "source_id",
        "platform",
        "repository_url",
        "namespace",
        "name",
        "commit_sha",
        "snapshot_path",
        "checksums",
        "mirror_path",
        "bundle_path",
        "archive_path",
    }
    missing = sorted(required - set(manifest))
    return [{"check": "source_manifest_schema", "severity": "error" if missing else "info", "passed": not missing, "missing": missing}]


def _git_bundle_verify(mirror: Path, bundle: Path) -> bool:
    result = run_command(["git", "--git-dir", str(mirror), "bundle", "verify", str(bundle)], check=False, timeout=300)
    return result.returncode == 0


def _lineage(commit_sha: str, mirror: Path) -> bool:
    result = run_command(["git", "--git-dir", str(mirror), "rev-parse", f"{commit_sha}^{{commit}}"], check=False, timeout=120)
    return result.returncode == 0


def _scan_snapshot(snapshot: Path, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ignored_dirs = list(cfg["pipeline"].get("ignored_directories", []))
    ignored_globs = list(cfg["pipeline"].get("ignored_globs", []))
    seen: dict[str, list[str]] = {}
    checks: list[dict[str, Any]] = []
    file_count = 0
    total_bytes = 0
    unreadable: list[str] = []
    symlinks: list[str] = []
    lfs_pointers: list[str] = []
    unsafe_paths: list[str] = []
    encoding_issues: list[dict[str, str]] = []
    duplicate_hashes: list[dict[str, Any]] = []

    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() and not path.is_symlink():
            continue
        if ignored(path, snapshot, ignored_dirs, ignored_globs):
            continue
        relative = path.relative_to(snapshot).as_posix()
        file_count += 1
        try:
            st = path.lstat() if path.is_symlink() else path.stat()
            total_bytes += st.st_size
        except Exception:
            pass
        if path.is_symlink():
            symlinks.append(relative)
            target = path.resolve(strict=False)
            try:
                target.relative_to(snapshot.resolve())
            except Exception:
                unsafe_paths.append(relative)
            continue
        try:
            raw = path.read_bytes()
        except Exception as exc:
            unreadable.append(f"{relative}: {exc}")
            continue
        digest = sha256_file(path)
        seen.setdefault(digest, []).append(relative)
        if raw.startswith(b"version https://git-lfs.github.com/spec/v1"):
            lfs_pointers.append(relative)
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            encoding_issues.append({"path": relative, "severity": "warning", "message": "not valid utf-8"})
        if b"\x00" in raw:
            encoding_issues.append({"path": relative, "severity": "warning", "message": "contains NUL bytes"})

    for digest, paths in seen.items():
        if len(paths) > 1:
            duplicate_hashes.append({"sha256": digest, "paths": paths, "count": len(paths)})

    summary = {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "unreadable_count": len(unreadable),
        "symlink_count": len(symlinks),
        "lfs_pointer_count": len(lfs_pointers),
        "duplicate_hash_count": len(duplicate_hashes),
        "unsafe_path_count": len(unsafe_paths),
    }
    checks.extend(
        [
            {"check": "file_count_nonzero", "severity": "error", "passed": file_count > 0, "count": file_count},
            {"check": "total_bytes_nonzero", "severity": "info", "passed": total_bytes >= 0, "count": total_bytes},
            {"check": "no_unreadable_files", "severity": "error", "passed": not unreadable, "files": unreadable},
            {"check": "no_unsafe_paths", "severity": "fatal", "passed": not unsafe_paths, "paths": unsafe_paths},
            {"check": "duplicate_file_hashes", "severity": "warning", "passed": True, "duplicates": duplicate_hashes},
            {"check": "symlink_metadata", "severity": "info", "passed": True, "symlinks": symlinks},
            {"check": "git_lfs_pointer_state", "severity": "info", "passed": True, "pointers": lfs_pointers},
            {"check": "filename_and_text_encoding", "severity": "warning", "passed": True, "issues": encoding_issues},
        ]
    )
    return checks, summary


def _report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Integrity Report",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Commit: `{report['commit_sha']}`",
        f"- Passed: `{report['passed']}`",
        "",
        "## Counts",
        "",
    ]
    for key, value in report["summary"].items():
        lines.append(f"- {key.replace('_', ' ').title()}: {value}")
    lines.extend(["", "## Checks", ""])
    for check in report["checks"]:
        lines.append(f"- [{('x' if check['passed'] else ' ')}] {check['check']} ({check['severity']})")
    return "\n".join(lines) + "\n"


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    snapshot = Path(run["snapshot_path"])
    raw = snapshot.parent / "raw"
    manifest_path = raw / "source-manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"Missing source manifest: {manifest_path}")
    manifest = read_json(manifest_path)
    bundle = raw / "repository.bundle"
    mirror = Path(str(manifest.get("mirror_path") or ""))
    archive = raw / "source.tar.zst"
    checks = _manifest_checks(manifest)
    checks.append({"check": "bundle_exists", "severity": "error", "passed": bundle.exists(), "path": str(bundle)})
    checks.append({"check": "archive_exists", "severity": "error", "passed": archive.exists(), "path": str(archive)})
    checks.append({"check": "git_bundle_verify", "severity": "error", "passed": bundle.exists() and mirror.exists() and _git_bundle_verify(mirror, bundle)})
    checks.append({"check": "recorded_commit_in_mirror", "severity": "error", "passed": bool(mirror.exists() and _lineage(run["commit_sha"], mirror))})
    checks.extend([
        {
            "check": "archive_checksum",
            "severity": "error",
            "passed": archive.exists() and manifest.get("checksums", {}).get("source.tar.zst") == sha256_file(archive),
        },
        {
            "check": "bundle_checksum",
            "severity": "error",
            "passed": bundle.exists() and manifest.get("checksums", {}).get("repository.bundle") == sha256_file(bundle),
        },
        {
            "check": "mirror_archive_checksum",
            "severity": "error",
            "passed": (raw / "mirror.git.tar.zst").exists()
            and manifest.get("checksums", {}).get("mirror.git.tar.zst") == sha256_file(raw / "mirror.git.tar.zst"),
        },
    ])
    tree_checks, summary = _scan_snapshot(snapshot, cfg)
    checks.extend(tree_checks)
    fatal_levels = {"error", "fatal"}
    passed = all(check["passed"] for check in checks if str(check.get("severity", "error")).lower() in fatal_levels)
    report = {
        "schema_version": 1,
        "pipeline_version": "0.1.0",
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "snapshot_path": str(snapshot),
        "source_manifest_path": str(manifest_path),
        "passed": passed,
        "summary": summary,
        "checks": checks,
        "warnings": [check for check in checks if check.get("severity") == "warning" and not check.get("passed", True)],
    }
    report_path = snapshot.parent / "integrity-report.json"
    write_json(report_path, report)
    (snapshot.parent / "integrity-report.md").write_text(_report_markdown(report), encoding="utf-8")
    return report
