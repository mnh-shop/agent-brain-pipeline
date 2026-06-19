from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.util import component_dir, ensure_analysis_workspace, run_command, write_json


def _format(args: list[Any], **values: str) -> list[str]:
    return [str(value).format(**values) for value in args]


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    section = cfg.get("codegraph", {})
    if not section.get("enabled", True):
        return {"passed": not section.get("required", True), "skipped": True}

    snapshot = Path(run["snapshot_path"])
    analysis_path = ensure_analysis_workspace(run, "codegraph")
    derived = component_dir(run, "codegraph")
    home = derived / "home"
    bundle_path = derived / "structure-graph.cgc"
    derived.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    command = str(section.get("command", "codegraphcontext"))
    values = {"snapshot_path": str(snapshot), "analysis_path": str(analysis_path), "bundle_path": str(bundle_path)}
    setup_args = _format(section.get("setup_args", ["config", "db", "kuzudb"]), **values)
    index_args = _format(section.get("index_args", ["index", "{analysis_path}", "--force"]), **values)
    smoke_args = _format(section.get("smoke_args", ["list"]), **values)
    stats_args = _format(section.get("stats_args", ["stats", "{analysis_path}"]), **values)
    export_args = _format(section.get("export_args", ["bundle", "export", "{bundle_path}", "--repo", "{analysis_path}"]), **values)
    timeout = int(cfg["pipeline"].get("stage_timeout_seconds", 7200))
    env = {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(derived / "cache"),
        "XDG_CONFIG_HOME": str(derived / "config"),
    }

    setup_result = run_command([command, *setup_args], env=env, timeout=timeout, check=False)
    index_result = run_command([command, *index_args], env=env, timeout=timeout, check=False)
    smoke_result = run_command([command, *smoke_args], env=env, timeout=timeout, check=False)
    stats_result = run_command([command, *stats_args], env=env, timeout=timeout, check=False)
    export_result = run_command([command, *export_args], env=env, timeout=timeout, check=False)

    passed = all(result.returncode == 0 for result in (setup_result, index_result, smoke_result, stats_result, export_result)) and bundle_path.exists()
    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "tool": "CodeGraphContext",
        "analysis_path": str(analysis_path),
        "storage_path": str(derived),
        "bundle_path": str(bundle_path),
        "commands": {
            "setup": [command, *setup_args],
            "index": [command, *index_args],
            "smoke": [command, *smoke_args],
            "stats": [command, *stats_args],
            "export": [command, *export_args],
        },
        "returncodes": {
            "setup": setup_result.returncode,
            "index": index_result.returncode,
            "smoke": smoke_result.returncode,
            "stats": stats_result.returncode,
            "export": export_result.returncode,
        },
        "stats_stdout_tail": stats_result.stdout[-20000:],
        "index_stdout_tail": index_result.stdout[-12000:],
        "index_stderr_tail": index_result.stderr[-12000:],
        "smoke_stdout_tail": smoke_result.stdout[-12000:],
        "export_stderr_tail": export_result.stderr[-12000:],
        "passed": passed,
    }
    report_path = snapshot.parent / "structure-report.json"
    write_json(report_path, report)
    if not passed and section.get("required", True):
        raise RuntimeError(f"CodeGraphContext stage failed; see {report_path}")
    return report
