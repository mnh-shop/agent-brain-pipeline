---
name: ingest-repository
description: Submit repository ingestion and check status.
---

# Ingest Repository

Use this skill when the user asks to ingest, import, index, analyze, refresh, or add a GitHub/GitLab repository.

## Procedure

1. Extract exactly one repository URL. Supported hosts are `github.com` and `gitlab.com`.
2. Create a Kanban task that includes the URL and these checklist stages: acquire, curate, structure, semantics, retrieval, audit, export.
3. Submit the deterministic pipeline run:

```bash
curl -fsS -X POST "$PIPELINE_API_URL/runs" \
  -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"REPOSITORY_URL\",\"trigger\":\"telegram\"}"
```

4. Save the returned `run_id` on the Kanban task.
5. Check status when asked:

```bash
curl -fsS "$PIPELINE_API_URL/runs/RUN_ID" \
  -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN"
```

6. Explain stage failures using the error and report path. Do not claim completion until status is `completed`.

