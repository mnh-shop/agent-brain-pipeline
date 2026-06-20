from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.db import PIPELINE_VERSION, record_stage_report
from pipeline.stages._semantics import normalize_semantic_outputs, parse_projects_output, probe_tool, _json_output, _project_from_index
from pipeline.util import component_dir, ensure_analysis_workspace, run_command, sha256_file, write_json


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _capture_result(name: str, result: Any, raw_dir: Path) -> tuple[Path, Path]:
    stdout_path = raw_dir / f"{name}.stdout.txt"
    stderr_path = raw_dir / f"{name}.stderr.txt"
    _write_text(stdout_path, result.stdout)
    _write_text(stderr_path, result.stderr)
    return stdout_path, stderr_path


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    section = cfg.get("codebase_memory", {})
    if not section.get("enabled", True):
        return {"passed": not section.get("required", True), "skipped": True}

    snapshot = Path(run["snapshot_path"])
    analysis_path = ensure_analysis_workspace(run, "codebase-memory")
    derived = component_dir(run, "codebase-memory")
    raw_dir = derived / "raw"
    normalized_dir = derived / "normalized"
    raw_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    command = str(section.get("command", "codebase-memory-mcp"))
    timeout = int(cfg["pipeline"].get("stage_timeout_seconds", 7200))
    env = {
        "CBM_CACHE_DIR": str(derived),
        "CBM_WORKERS": str(section.get("workers", 4)),
        "CBM_LOG_LEVEL": "info",
    }
    probe = probe_tool(command, env, timeout)

    index_payload = {
        "repo_path": str(analysis_path),
        "mode": str(section.get("mode", "full")),
        "persistence": True,
    }
    index_result = run_command([command, "cli", "index_repository", json.dumps(index_payload)], env=env, timeout=timeout, check=False)
    index_stdout_path, index_stderr_path = _capture_result("index_repository", index_result, raw_dir)
    index_output = _json_output(index_result.stdout)
    project = _project_from_index(index_output, str(section.get("project_name", analysis_path.name)))

    list_result = run_command([command, "cli", "list_projects"], env=env, timeout=timeout, check=False)
    list_stdout_path, list_stderr_path = _capture_result("list_projects", list_result, raw_dir)

    architecture_result = run_command([command, "cli", "get_architecture", json.dumps({"project": project})], env=env, timeout=timeout, check=False)
    architecture_stdout_path, architecture_stderr_path = _capture_result("architecture", architecture_result, raw_dir)

    graph_smoke = run_command([command, "cli", "search_graph", json.dumps({"project": project, "limit": 3})], env=env, timeout=timeout, check=False)
    graph_stdout_path, graph_stderr_path = _capture_result("graph_smoke", graph_smoke, raw_dir)

    semantic_payload = {"project": project, "query": "repository architecture", "limit": 3}
    semantic_command = [command, "cli", "semantic_query", json.dumps(semantic_payload)]
    semantic_query_supported = "semantic_query" in probe.supported_commands
    semantic_smoke = None
    if semantic_query_supported:
        semantic_smoke = run_command(semantic_command, env=env, timeout=timeout, check=False)
        if semantic_smoke.returncode != 0 and "unknown tool: semantic_query" in semantic_smoke.stderr.lower():
            semantic_query_supported = False
    if semantic_smoke is not None:
        semantic_stdout_path, semantic_stderr_path = _capture_result("semantic_smoke", semantic_smoke, raw_dir)
    else:
        semantic_stdout_path = raw_dir / "semantic_smoke.stdout.txt"
        semantic_stderr_path = raw_dir / "semantic_smoke.stderr.txt"
        _write_text(semantic_stdout_path, "")
        _write_text(semantic_stderr_path, "semantic_query unsupported by installed codebase-memory-mcp; skipped\n")

    artifact = analysis_path / ".codebase-memory" / "graph.db.zst"
    if artifact.exists():
        shutil.copy2(artifact, raw_dir / artifact.name)

    normalized = normalize_semantic_outputs(
        run={**run, "pipeline_version": run.get("pipeline_version", PIPELINE_VERSION)},
        snapshot=snapshot,
        raw_dir=raw_dir,
        normalized_dir=normalized_dir,
        index_output=index_output,
        projects_output=_json_output(list_result.stdout),
        architecture_output=_json_output(architecture_result.stdout),
        graph_output=_json_output(graph_smoke.stdout),
        semantic_output=_json_output(semantic_smoke.stdout) if semantic_smoke is not None else None,
        probe=probe,
        command_results={
            "index": {"index_mode": index_payload["mode"], "workers": int(section.get("workers", 4)), "cache_dir": str(derived)},
            "list_projects": {"path": str(list_stdout_path)},
            "graph_search": {"returncode": graph_smoke.returncode},
            "semantic_query": {
                "supported": semantic_query_supported,
                "invoked": semantic_smoke is not None,
                "returncode": semantic_smoke.returncode if semantic_smoke is not None else None,
            },
        },
    )

    projects = parse_projects_output(_json_output(list_result.stdout))
    projects_found = any(
        str(item.get(key, "")).strip() == project
        for item in projects
        for key in ("project", "project_id", "project_name", "name")
    )
    graph_artifact_ok = artifact.exists() and artifact.stat().st_size > 0
    semantic_command_ok = (not semantic_query_supported) or (semantic_smoke is not None and semantic_smoke.returncode == 0)
    passed = bool(
        index_result.returncode == 0
        and list_result.returncode == 0
        and architecture_result.returncode == 0
        and graph_smoke.returncode == 0
        and semantic_command_ok
        and projects_found
        and graph_artifact_ok
        and normalized["manifest"]["passed"]
    )

    raw_manifest = {
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "project": project,
        "index_payload": index_payload,
        "artifact": str(artifact) if artifact.exists() else None,
        "artifact_sha256": sha256_file(artifact) if artifact.exists() else None,
        "commands": {
            "index_repository": [command, "cli", "index_repository", json.dumps(index_payload)],
            "list_projects": [command, "cli", "list_projects"],
            "get_architecture": [command, "cli", "get_architecture", json.dumps({"project": project})],
            "search_graph": [command, "cli", "search_graph", json.dumps({"project": project, "limit": 3})],
            "semantic_query": semantic_command,
        },
        "supports": probe.supported_commands,
        "semantic_query_supported": semantic_query_supported,
    }
    write_json(raw_dir / "manifest.json", raw_manifest)
    write_json(normalized_dir / "manifest.json", normalized["manifest"])
    write_json(normalized_dir / "project.json", normalized["project_record"])
    write_json(normalized_dir / "architecture.json", normalized["architecture"])
    if normalized["semantic_records"]:
        (normalized_dir / "semantic-smoke-results.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in normalized["semantic_records"]),
            encoding="utf-8",
        )
    if normalized["graph_records"]:
        (normalized_dir / "graph-smoke-results.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in normalized["graph_records"]),
            encoding="utf-8",
        )
    if normalized["symbol_records"]:
        (normalized_dir / "symbols.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in normalized["symbol_records"]),
            encoding="utf-8",
        )
    if normalized["relationship_records"]:
        (normalized_dir / "relationships.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in normalized["relationship_records"]),
            encoding="utf-8",
        )

    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "tool": "codebase-memory-mcp",
        "project": project,
        "index_mode": index_payload["mode"],
        "workers": int(section.get("workers", 4)),
        "version": probe.version,
        "executable": probe.executable,
        "executable_hash": probe.executable_hash,
        "cache_dir": str(derived),
        "analysis_path": str(analysis_path),
        "storage_path": str(derived),
        "raw_path": str(raw_dir),
        "normalized_path": str(normalized_dir),
        "portable_graph_artifact": str(artifact) if artifact.exists() else None,
        "artifact_sha256": sha256_file(artifact) if artifact.exists() else None,
        "projects": projects,
        "project_in_projects": projects_found,
        "semantic_query_supported": semantic_query_supported,
        "architecture": _json_output(architecture_result.stdout),
        "graph_smoke": _json_output(graph_smoke.stdout),
        "semantic_smoke": _json_output(semantic_smoke.stdout) if semantic_smoke is not None else None,
        "returncodes": {
            "index_repository": index_result.returncode,
            "list_projects": list_result.returncode,
            "get_architecture": architecture_result.returncode,
            "search_graph": graph_smoke.returncode,
            "semantic_query": semantic_smoke.returncode if semantic_smoke is not None else None,
        },
        "command_artifacts": {
            "index_repository": {"stdout": str(index_stdout_path), "stderr": str(index_stderr_path)},
            "list_projects": {"stdout": str(list_stdout_path), "stderr": str(list_stderr_path)},
            "get_architecture": {"stdout": str(architecture_stdout_path), "stderr": str(architecture_stderr_path)},
            "search_graph": {"stdout": str(graph_stdout_path), "stderr": str(graph_stderr_path)},
            "semantic_query": {"stdout": str(semantic_stdout_path), "stderr": str(semantic_stderr_path)},
        },
        "normalized": normalized["manifest"],
        "passed": passed,
    }
    report_path = snapshot.parent / "codebase-memory-report.json"
    write_json(report_path, report)
    write_json(snapshot.parent / "semantic-report.json", report)
    (snapshot.parent / "codebase-memory-report.md").write_text(
        "\n".join(
            [
                "# Codebase-Memory report",
                "",
                f"- Project: {project}",
                f"- Version: {probe.version or 'unknown'}",
                f"- Cache: {derived}",
                f"- Artifact: {artifact}",
                f"- Semantic results: {len(normalized['semantic_records'])}",
                f"- Graph results: {len(normalized['graph_records'])}",
                f"- Matched canonical records: {len([row for row in normalized['symbol_records'] if row['matched']])}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    record_stage_report({
        "run_id": run["run_id"],
        "stage": "semantic",
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "status": "passed" if passed else "failed",
        "passed": passed,
        "summary": {"project": project},
        "metrics": report,
        "warnings": (
            []
            if passed and semantic_query_supported
            else ["semantic_query unsupported by installed codebase-memory-mcp; graph-only semantics validation used"]
            if passed
            else ["Semantic normalization produced validation failures"]
        ),
        "errors": [] if passed else [{"stage": "semantic", "returncodes": report["returncodes"]}],
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
    })
    if not passed and section.get("required", True):
        raise RuntimeError(f"Codebase-Memory stage failed; see {report_path}")
    return report
