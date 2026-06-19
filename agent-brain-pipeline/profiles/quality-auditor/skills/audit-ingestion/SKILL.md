---
name: audit-ingestion
description: Review final deterministic audit results.
---

# Audit Ingestion

Inspect the `audit` stage and `audit-report.json`. A completed ingestion must include the Git bundle, raw snapshot, checksums, curation report, CodeGraph report, Codebase-Memory report, retrieval report, and passing exact/FTS smoke checks.

If failed, identify the exact check and create a repair task assigned to the stage owner.

