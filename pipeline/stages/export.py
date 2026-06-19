from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.config import obsidian_dir
from pipeline.db import connect
from pipeline.urls import parse_repository_url
from pipeline.util import read_json, utc_now


def _safe_note(value: str) -> str:
    return value.replace("/", " - ").replace("\\", " - ")


def run(run: dict[str, Any]) -> dict[str, Any]:
    vault = obsidian_dir()
    repo = parse_repository_url(run["repository_url"])
    source_dir = vault / "10 Sources" / ("GitHub" if repo.platform == "github" else "GitLab")
    report_dir = vault / "80 Reports"
    source_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    snapshot = Path(run["snapshot_path"])
    reports = {}
    report_files = {
        "integrity": "integrity-report.json",
        "normalize": "normalization-report.json",
        "lint": "lint-report.json",
        "structure": "structure-report.json",
        "semantic": "semantic-report.json",
        "retrieval": "retrieval-report.json",
        "audit": "audit-report.json",
        "curate": "curate-report.json",
    }
    for name, filename in report_files.items():
        path = snapshot.parent / filename
        if path.exists():
            reports[name] = read_json(path)

    title = f"{repo.namespace} - {repo.name}"
    source_note = source_dir / f"{_safe_note(title)}.md"
    content = f"""---
source_id: {run['source_id']}
platform: {repo.platform}
repository: {repo.normalized}
commit: {run['commit_sha']}
last_ingested: {utc_now()}
status: verified-and-indexed
---

# {repo.namespace}/{repo.name}

## Source

- Repository: {repo.normalized}
- Commit: `{run['commit_sha']}`
- Snapshot: `{run['snapshot_path']}`
- Run: `{run['run_id']}`

## Pipeline status

- Raw source preserved: yes
- Integrity: {'passed' if reports.get('integrity', {}).get('passed') else 'failed'}
- Normalize: {'passed' if reports.get('normalize', {}).get('passed') else 'failed'}
- Lint: {'passed' if reports.get('lint', {}).get('passed') else 'failed'}
- Compatibility curate alias: {'passed' if reports.get('curate', {}).get('passed') else 'available'}
- CodeGraphContext: {'passed' if reports.get('structure', {}).get('passed') else 'failed'}
- Codebase-Memory: {'passed' if reports.get('semantic', {}).get('passed') else 'failed'}
- Full-text retrieval: {'passed' if reports.get('retrieval', {}).get('passed') else 'failed'}
- Quality audit: {'passed' if reports.get('audit', {}).get('passed') else 'failed'}

## Counts

- Files: {reports.get('curate', {}).get('file_count', 0)}
- Text files: {reports.get('curate', {}).get('text_file_count', 0)}
- Binary files preserved: {reports.get('curate', {}).get('binary_file_count', 0)}
- Searchable units: {reports.get('curate', {}).get('unit_count', 0)}
- Duplicate files: {reports.get('curate', {}).get('duplicate_file_count', 0)}

## Retrieval

Available methods:

- Exact source search
- SQLite FTS5 full-text search
- CodeGraphContext structural search
- Codebase-Memory semantic search
- Hybrid search

## Reports

- [[{_safe_note(title)} - Ingestion Report]]
"""
    source_note.write_text(content, encoding="utf-8")

    report_note = report_dir / f"{_safe_note(title)} - Ingestion Report.md"
    report_note.write_text(
        f"# {repo.namespace}/{repo.name} — Ingestion Report\n\n"
        f"- Run: `{run['run_id']}`\n- Commit: `{run['commit_sha']}`\n\n"
        "```json\n" + json.dumps(reports, indent=2, ensure_ascii=False) + "\n```\n",
        encoding="utf-8",
    )

    refresh_kanban(vault)
    return {"passed": True, "source_note": str(source_note), "report_note": str(report_note)}


def refresh_kanban(vault: Path | None = None) -> None:
    vault = vault or obsidian_dir()
    board = vault / "Ingestion Kanban.md"
    with connect() as connection:
        rows = connection.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 100").fetchall()
    groups: dict[str, list[Any]] = {"Queued": [], "Running": [], "Failed": [], "Complete": []}
    for row in rows:
        status = row["status"]
        if status == "completed": group = "Complete"
        elif status == "failed": group = "Failed"
        elif status in {"running"}: group = "Running"
        else: group = "Queued"
        groups[group].append(row)
    lines = ["# Ingestion Kanban", "", "> Generated from pipeline.sqlite. Do not use this note as the execution source of truth.", ""]
    for group in ("Queued", "Running", "Failed", "Complete"):
        lines.extend([f"## {group}", ""])
        if not groups[group]:
            lines.append("- _None_")
        for row in groups[group]:
            lines.append(f"- **{row['run_id']}** — {row['repository_url']} — `{row['current_stage'] or row['status']}`")
        lines.append("")
    board.write_text("\n".join(lines), encoding="utf-8")
