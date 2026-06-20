#!/usr/bin/env python3
"""Small dependency-free client for the Agent Brain Pipeline API."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

STAGE_ALIASES = {
    "curate": "normalize",
}


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    base = os.environ.get("PIPELINE_API_URL", "http://knowledge-pipeline:8080").rstrip("/")
    token = os.environ.get("AGENT_BRAIN_API_TOKEN", "")
    if not token:
        raise SystemExit("AGENT_BRAIN_API_TOKEN is not configured")
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{base}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Pipeline API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach Pipeline API: {exc.reason}") from exc


def main() -> None:
    parser = argparse.ArgumentParser(prog="brain-api")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Queue a GitHub or GitLab repository")
    ingest.add_argument("url")
    ingest.add_argument("--ref")
    ingest.add_argument("--trigger", default="telegram")

    status = sub.add_parser("status", help="Read one run and its stage states")
    status.add_argument("run_id")

    listing = sub.add_parser("list", help="List recent runs")
    listing.add_argument("--limit", type=int, default=20)

    report = sub.add_parser("report", help="Read a machine-generated stage report")
    report.add_argument("run_id")
    report.add_argument("stage", choices=["acquire", "curate", "integrity", "normalize", "lint", "syntax", "structure", "semantics", "retrieval", "vector", "audit", "export"])

    retry = sub.add_parser("retry", help="Retry a failed run from the beginning")
    retry.add_argument("run_id")

    search = sub.add_parser("search", help="Search completed ingestions")
    search.add_argument("query")
    search.add_argument("--mode", choices=["exact", "fts", "structural", "semantic", "hybrid"], default="hybrid")
    search.add_argument("--source-id")
    search.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    if args.command == "ingest":
        result = request("POST", "/runs", {"url": args.url, "ref": args.ref, "trigger": args.trigger})
    elif args.command == "status":
        result = request("GET", f"/runs/{urllib.parse.quote(args.run_id, safe='')}")
    elif args.command == "list":
        result = request("GET", f"/runs?limit={max(1, min(args.limit, 200))}")
    elif args.command == "report":
        run_id = urllib.parse.quote(args.run_id, safe="")
        stage = urllib.parse.quote(STAGE_ALIASES.get(args.stage, args.stage), safe="")
        result = request("GET", f"/runs/{run_id}/reports/{stage}")
    elif args.command == "retry":
        result = request("POST", f"/runs/{urllib.parse.quote(args.run_id, safe='')}/retry")
    else:
        result = request("POST", "/search", {
            "query": args.query,
            "mode": args.mode,
            "source_id": args.source_id,
            "limit": args.limit,
        })
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
