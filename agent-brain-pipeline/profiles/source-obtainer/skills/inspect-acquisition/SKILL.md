---
name: inspect-acquisition
description: Inspect source acquisition status and raw artifacts.
---

# Inspect Acquisition

Use the pipeline run endpoint and inspect the `acquire` stage. Confirm that the run records a source ID, exact commit SHA, Git bundle, compressed source archive, checksums, and source manifest.

```bash
curl -fsS "$PIPELINE_API_URL/runs/RUN_ID" -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN"
```

Never read or display repository access tokens.

