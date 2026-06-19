from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.util import component_dir, ensure_analysis_workspace, run_command, write_json


def _json_output(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value[-20000:]


def _project_from_index(index_output: Any, fallback: str) -> str:
    """Extract the canonical project ID returned by Codebase-Memory."""
    if isinstance(index_output, dict):
        for key in ("project", "project_name", "name"):
            value = index_output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in index_output.values():
            found = _project_from_index(value, "")
            if found:
                return found
    if isinstance(index_output, list):
        for value in index_output:
            found = _project_from_index(value, "")
            if found:
                return found
    return fallback


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    section = cfg.get("codebase_memory", {})
    if not section.get("enabled", True):
        return {"passed": not section.get("required", True), "skipped": True}

    snapshot = Path(run["snapshot_path"])
    analysis_path = ensure_analysis_workspace(run, "codebase-memory")
    derived = component_dir(run, "codebase-memory")
    derived.mkdir(parents=True, exist_ok=True)
    command = str(section.get("command", "codebase-memory-mcp"))
    timeout = int(cfg["pipeline"].get("stage_timeout_seconds", 7200))
    env = {
        "CBM_CACHE_DIR": str(derived),
        "CBM_WORKERS": str(section.get("workers", 4)),
        "CBM_LOG_LEVEL": "info",
    }

    # Full mode is intentional: semantic_query depends on the complete semantic
    # pass, and this pipeline prioritizes data quality over fastest indexing.
    index_payload = {
        "repo_path": str(analysis_path),
        "mode": str(section.get("mode", "full")),
        "persistence": True,
    }
    index_result = run_command(
        [command, "cli", "index_repository", json.dumps(index_payload)],
        env=env,
        timeout=timeout,
        check=False,
    )
    index_output = _json_output(index_result.stdout)
    fallback_project = str(section.get("project_name", analysis_path.name))
    project = _project_from_index(index_output, fallback_project)

    list_result = run_command([command, "cli", "list_projects"], env=env, timeout=timeout, check=False)
    architecture_result = run_command(
        [command, "cli", "get_architecture", json.dumps({"project": project})],
        env=env,
        timeout=timeout,
        check=False,
    )
    graph_smoke = run_command(
        [command, "cli", "search_graph", json.dumps({"project": project, "limit": 3})],
        env=env,
        timeout=timeout,
        check=False,
    )
    semantic_smoke = run_command(
        [command, "cli", "semantic_query", json.dumps({"project": project, "query": "repository architecture", "limit": 3})],
        env=env,
        timeout=timeout,
        check=False,
    )
    artifact = analysis_path / ".codebase-memory" / "graph.db.zst"
    passed = all(result.returncode == 0 for result in (index_result, list_result, architecture_result, graph_smoke, semantic_smoke))
    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "tool": "codebase-memory-mcp",
        "project": project,
        "index_mode": index_payload["mode"],
        "analysis_path": str(analysis_path),
        "storage_path": str(derived),
        "portable_graph_artifact": str(artifact) if artifact.exists() else None,
        "returncodes": {
            "index": index_result.returncode,
            "list_projects": list_result.returncode,
            "architecture": architecture_result.returncode,
            "graph_smoke": graph_smoke.returncode,
            "semantic_smoke": semantic_smoke.returncode,
        },
        "index_result": index_output,
        "projects": _json_output(list_result.stdout),
        "architecture": _json_output(architecture_result.stdout),
        "graph_smoke": _json_output(graph_smoke.stdout),
        "semantic_smoke": _json_output(semantic_smoke.stdout),
        "index_stderr_tail": index_result.stderr[-12000:],
        "passed": passed,
    }
    report_path = snapshot.parent / "semantic-report.json"
    write_json(report_path, report)
    if not passed and section.get("required", True):
        raise RuntimeError(f"Codebase-Memory stage failed; see {report_path}")
    return report
