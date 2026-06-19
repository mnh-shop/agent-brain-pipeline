---
name: maintain-sources
description: Inspect refresh scheduling and retry failed maintenance runs.
---

# Maintain Sources

The pipeline scheduler automatically queues due repositories using `maintenance.refresh_interval_hours` and jitter from `config/runtime.yaml`.

List recent runs:

```bash
curl -fsS "$PIPELINE_API_URL/runs?limit=50" -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN"
```

Retry a failed run only after reading its failed stage:

```bash
curl -fsS -X POST "$PIPELINE_API_URL/runs/RUN_ID/retry" -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN"
```

