from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pipeline.config import get_config
from pipeline.db import PIPELINE_VERSION, record_stage_report, record_tool_execution
from pipeline.stages._structure import ToolProbe, normalize_graph, probe_tool
from pipeline.util import component_dir, ensure_analysis_workspace, run_command, sha256_text, write_json


def _format(args: list[Any], **values: str) -> list[str]:
    return [str(value).format(**values) for value in args]


def _copy_path(src: Path, dst: Path) -> None:
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _tool_env(derived: Path) -> dict[str, str]:
    return {
        "HOME": str(derived / "home"),
        "XDG_CACHE_HOME": str(derived / "cache"),
        "XDG_CONFIG_HOME": str(derived / "config"),
    }


def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    section = cfg.get("codegraph", {})
    if not section.get("enabled", True):
        return {"passed": not section.get("required", True), "skipped": True}

    snapshot = Path(run["snapshot_path"])
    analysis_path = ensure_analysis_workspace(run, "codegraph")
    derived = component_dir(run, "codegraph")
    raw_dir = derived / "raw"
    normalized_dir = derived / "normalized"
    home = derived / "home"
    raw_dir.mkdir(parents=True, exist_ok=True)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    command = str(section.get("command", "codegraphcontext"))
    timeout = int(cfg["pipeline"].get("stage_timeout_seconds", 7200))
    env = _tool_env(derived)
    probe = probe_tool(command, env, timeout)

    values = {"snapshot_path": str(snapshot), "analysis_path": str(analysis_path), "bundle_path": str(raw_dir / "structure-graph.cgc")}
    setup_args = _format(section.get("setup_args", ["config", "db", "kuzudb"]), **values)
    index_args = _format(section.get("index_args", ["index", "{analysis_path}", "--force"]), **values)
    smoke_args = _format(section.get("smoke_args", ["list"]), **values)
    stats_args = _format(section.get("stats_args", ["stats", "{analysis_path}"]), **values)
    export_args = _format(section.get("export_args", ["bundle", "export", "{bundle_path}", "--repo", "{analysis_path}"]), **values)
    find_args = _format(section.get("find_args", ["find", "pattern", "{query}"]), **values)

    command_results: dict[str, dict[str, Any]] = {}
    query_records: list[dict[str, Any]] = []

    def _run_logged(name: str, args: list[str], *, query: str | None = None) -> Any:
        start_args = [command, *args]
        result = run_command(start_args, env=env, timeout=timeout, check=False)
        stdout_path = raw_dir / f"{name}.stdout.txt"
        stderr_path = raw_dir / f"{name}.stderr.txt"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        command_results[name] = {
            "command": start_args,
            "returncode": result.returncode,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        record_tool_execution(
            {
                "run_id": run["run_id"],
                "stage": "structure",
                "tool_name": "CodeGraphContext",
                "tool_version": probe.version or "unknown",
                "status": "passed" if result.returncode == 0 else "failed",
                "command": json.dumps(start_args, ensure_ascii=False),
                "input_json": json.dumps({"query": query} if query is not None else {}, ensure_ascii=False),
                "output_json": json.dumps({"stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}, ensure_ascii=False),
                "schema_version": 1,
                "pipeline_version": PIPELINE_VERSION,
            }
        )
        if query is not None:
            query_records.append(
                {
                    "query": query,
                    "command": start_args,
                    "returncode": result.returncode,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                }
            )
        return result

    setup_result = _run_logged("setup", setup_args)
    index_result = _run_logged("index", index_args)
    smoke_result = _run_logged("smoke", smoke_args)
    stats_result = _run_logged("stats", stats_args)
    export_result = _run_logged("export", export_args)

    bundle_path = raw_dir / "structure-graph.cgc"
    _copy_path(bundle_path, raw_dir / "structure-graph.cgc")
    _copy_path(raw_dir / "structure-graph.cgc", derived / "structure-graph.cgc")

    query_args = list(find_args)
    query_probes = [
        {"name": "symbol", "query": "helper"},
        {"name": "import", "query": "node:fs"},
    ]
    for probe_query in query_probes:
        args = [part.format(query=probe_query["query"]) for part in query_args]
        _run_logged(f"query-{probe_query['name']}", args, query=probe_query["query"])

    syntax_dir = snapshot.parent / "syntax"
    normalization_dir = snapshot.parent / "normalization"
    files_path = normalization_dir / "files.jsonl"
    syntax_files = []
    if files_path.exists():
        syntax_files = [json.loads(line) for line in files_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    syntax_symbols_path = syntax_dir / "symbols.jsonl"
    syntax_symbols = []
    if syntax_symbols_path.exists():
        syntax_symbols = [json.loads(line) for line in syntax_symbols_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    manifest = normalize_graph(
        run=run,
        snapshot=snapshot,
        raw_dir=raw_dir,
        normalized_dir=normalized_dir,
        probe=probe,
        command_results=command_results,
        syntax_symbols=syntax_symbols,
        syntax_files=syntax_files,
    )

    report = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "tool": "CodeGraphContext",
        "database_type": "kuzu",
        "codegraph_version": probe.version,
        "executable": probe.executable,
        "executable_hash": probe.executable_hash,
        "supported_commands": probe.supported_commands,
        "analysis_path": str(analysis_path),
        "storage_path": str(derived),
        "raw_path": str(raw_dir),
        "normalized_path": str(normalized_dir),
        "bundle_path": str(bundle_path),
        "commands": command_results,
        "returncodes": {name: result["returncode"] for name, result in command_results.items()},
        "stats_stdout_tail": (raw_dir / "stats.stdout.txt").read_text(encoding="utf-8")[-20000:] if (raw_dir / "stats.stdout.txt").exists() else "",
        "index_stdout_tail": (raw_dir / "index.stdout.txt").read_text(encoding="utf-8")[-12000:] if (raw_dir / "index.stdout.txt").exists() else "",
        "index_stderr_tail": (raw_dir / "index.stderr.txt").read_text(encoding="utf-8")[-12000:] if (raw_dir / "index.stderr.txt").exists() else "",
        "smoke_stdout_tail": (raw_dir / "smoke.stdout.txt").read_text(encoding="utf-8")[-12000:] if (raw_dir / "smoke.stdout.txt").exists() else "",
        "export_stderr_tail": (raw_dir / "export.stderr.txt").read_text(encoding="utf-8")[-12000:] if (raw_dir / "export.stderr.txt").exists() else "",
        "config_hash": sha256_text(json.dumps(section, sort_keys=True, ensure_ascii=False)),
        "normalized_manifest": manifest,
        "queries": query_records,
        "passed": bool(
            bundle_path.exists()
            and all(result.returncode == 0 for result in (setup_result, index_result, smoke_result, stats_result, export_result))
            and manifest["node_count"] > 0
            and manifest["edge_count"] >= 0
            and manifest["symbol_match_count"] > 0
            and any(item["passed"] for item in query_records)
        ),
    }
    report_path = snapshot.parent / "codegraph-report.json"
    write_json(report_path, report)
    (snapshot.parent / "codegraph-report.md").write_text(
        "\n".join(
            [
                "# CodeGraphContext report",
                "",
                f"- Version: {probe.version or 'unknown'}",
                f"- Executable: {probe.executable}",
                f"- Bundle: {bundle_path}",
                f"- Nodes: {manifest['node_count']}",
                f"- Edges: {manifest['edge_count']}",
                f"- Symbol matches: {manifest['symbol_match_count']}",
                f"- Unmatched nodes: {manifest['unmatched_count']}",
                f"- Queries: {manifest['query_count']}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # compatibility alias
    write_json(snapshot.parent / "structure-report.json", report)

    record_stage_report({
        "run_id": run["run_id"],
        "stage": "structure",
        "source_id": run["source_id"],
        "commit_sha": run["commit_sha"],
        "status": "passed" if report["passed"] else "failed",
        "passed": report["passed"],
        "summary": {"tool": report["tool"], "node_count": manifest["node_count"], "edge_count": manifest["edge_count"]},
        "metrics": report,
        "warnings": [],
        "errors": [] if report["passed"] else [{"stage": "structure", "returncodes": report["returncodes"]}],
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
    })

    if not report["passed"] and section.get("required", True):
        raise RuntimeError(f"CodeGraphContext stage failed; see {report_path}")
    return report
