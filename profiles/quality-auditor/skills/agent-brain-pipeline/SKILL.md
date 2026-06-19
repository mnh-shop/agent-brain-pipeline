---
name: agent-brain-audit
description: Inspect deterministic audit reports for raw preservation, mandatory graphs, indexes, and provenance.
version: 0.1.0
author: Agent Brain Pipeline
license: MIT
platforms:
  - linux
metadata:
  hermes:
    tags:
      - agent-brain
      - repository-ingestion
      - codegraph
      - codebase-memory
---

# Agent Brain Pipeline

Use the deterministic Pipeline API through the bundled client. Never print `AGENT_BRAIN_API_TOKEN` or other secrets.

Set the client path once per terminal command:

```bash
CLIENT="${HERMES_HOME:-$HOME/.hermes}/skills/agent-brain-pipeline/scripts/brain_api.py"
```

## Commands

```bash
python3 "$CLIENT" ingest https://github.com/OWNER/REPO
python3 "$CLIENT" ingest https://gitlab.com/GROUP/REPO --ref main
python3 "$CLIENT" status INGEST-XXXXXXXXXXXX
python3 "$CLIENT" list --limit 20
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX acquire
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX structure
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX semantics
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX audit
python3 "$CLIENT" retry INGEST-XXXXXXXXXXXX
python3 "$CLIENT" search "how are profiles loaded?" --mode hybrid
```

## Required workflow

Use `brain-api report <RUN_ID> audit`. A run is not acceptable when checksums, required artifacts, CodeGraphContext, Codebase-Memory, FTS, or exact-search smoke tests failed. Do not modify the artifacts under review. Report failed check names and recommend the responsible stage/profile.

## Safety and data boundaries

- The Pipeline API validates GitHub/GitLab URLs.
- The durable worker runs all deterministic stages and survives chat-session termination.
- SQLite is the source of truth; the Obsidian Kanban mirrors it.
- Profiles supervise and interpret. They do not replace Git, Python, CodeGraphContext, Codebase-Memory, or checksums.
