---
name: search-knowledge
description: Search indexed repositories with multiple methods.
---

# Search Knowledge

Modes:

- `exact`: literal source search with ripgrep.
- `fts`: SQLite FTS5 over curated units.
- `structural`: CodeGraphContext.
- `semantic`: Codebase-Memory.
- `hybrid`: all methods together.

```bash
curl -fsS -X POST "$PIPELINE_API_URL/search" \
  -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query":"QUESTION","mode":"hybrid","source_id":null,"limit":10}'
```

Do not convert weak semantic similarity into a definitive statement without direct source evidence.

