---
name: agent-brain-orchestration
description: Queue GitHub/GitLab ingestions, inspect runs, retry failures, and search completed knowledge from Telegram.
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
Do not edit this skill, `brain_api.py`, or any live file under `~/.hermes/skills` or `~/.hermes/profiles`; those files are deployment artifacts synchronized from this repository.
For orchestrator work, use only the bundled client in this skill. Do not execute scripts from worker-profile directories.
For durable work ownership, use Hermes Kanban. Do not use `todo` or `cronjob` as a substitute for Kanban lifecycle.

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
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX integrity
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX normalize
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX lint
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX syntax
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX structure
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX semantics
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX retrieval
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX vector
python3 "$CLIENT" report INGEST-XXXXXXXXXXXX audit
python3 "$CLIENT" retry INGEST-XXXXXXXXXXXX
python3 "$CLIENT" search "how are profiles loaded?" --mode hybrid
```

## Required workflow

When the user provides a GitHub or GitLab repository URL:

1. Create or update one Kanban task for the request.
2. Run `brain-api ingest <URL>` exactly once.
3. Add the returned run ID to the Kanban task.
4. Return the run ID immediately and explain that the durable pipeline worker owns execution.
5. Use `brain-api status <RUN_ID>` when the user asks for progress.
6. Do not clone or analyze the repository in the Hermes container.

For knowledge questions, use hybrid search by default. Use exact search only for literal identifiers, configuration keys, or error messages.

If the user asks for status-change notifications, keep the Kanban task updated and report the current pipeline status. Do not create cron jobs or other background polling loops unless a dedicated notification flow exists.

## Safety and data boundaries

- The Pipeline API validates GitHub/GitLab URLs.
- The durable worker runs all deterministic stages and survives chat-session termination.
- SQLite is the source of truth; the Obsidian Kanban mirrors it.
- Profiles supervise and interpret. They do not replace Git, Python, CodeGraphContext, Codebase-Memory, or checksums.
