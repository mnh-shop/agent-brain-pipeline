---
name: ingest-repository
description: Submit repository ingestion and check status.
---

# Ingest Repository

Use this skill when the user asks to ingest, import, index, analyze, refresh, or add a GitHub/GitLab repository.

## Procedure

1. Extract exactly one repository URL. Supported hosts are `github.com` and `gitlab.com`.
2. Create a Kanban task that includes the URL and these checklist stages: acquire, integrity, normalize, lint, syntax, structure, semantics, retrieval, vector, audit, export.
3. Submit the deterministic pipeline run exactly once:

```bash
CLIENT="${HERMES_HOME:-$HOME/.hermes}/skills/agent-brain-pipeline/scripts/brain_api.py"
python3 "$CLIENT" ingest REPOSITORY_URL
```

4. Save the returned `run_id` on the Kanban task.
5. Check status when asked:

```bash
CLIENT="${HERMES_HOME:-$HOME/.hermes}/skills/agent-brain-pipeline/scripts/brain_api.py"
python3 "$CLIENT" status RUN_ID
```

6. Explain stage failures using the error and report path. Do not claim completion until status is `ready_for_wiki`.
7. Do not use `todo` or `cronjob` for ingestion lifecycle management.
