---
name: inspect-codebase-memory
description: Inspect Codebase-Memory and semantic search.
---

# Inspect Codebase-Memory

Check the semantics stage in the run report. For semantic retrieval:

```bash
curl -fsS -X POST "$PIPELINE_API_URL/search" \
  -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"QUESTION","mode":"semantic","source_id":"SOURCE_ID","limit":12}'
```

Always retain repository and commit metadata when presenting results.

