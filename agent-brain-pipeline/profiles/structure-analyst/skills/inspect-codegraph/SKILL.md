---
name: inspect-codegraph
description: Inspect CodeGraphContext results and structural search.
---

# Inspect CodeGraph

Check the structure stage in the run report. For a structural search:

```bash
curl -fsS -X POST "$PIPELINE_API_URL/search" \
  -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"PATTERN","mode":"structural","source_id":"SOURCE_ID","limit":10}'
```

Use structural results for definitions, functions, classes, calls, imports, inheritance, and dependencies.

