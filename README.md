# Agent Brain Pipeline

A deployable Hermes Agent pipeline for turning GitHub and GitLab repositories into preserved, verified, graph-indexed, searchable knowledge.

## What this repository does

You message the Hermes orchestrator on Telegram:

```text
Please ingest https://github.com/owner/repository
```

The orchestrator creates a Kanban task and submits one ingestion run. The deterministic pipeline then performs:

1. **Acquire** — clone the repository, pin the commit, save a Git bundle and compressed raw snapshot.
2. **Integrity** — verify hashes, bundle contents, path safety, and source manifests.
3. **Normalize** — classify files, detect encodings and duplicates, and create canonical units.
4. **Lint** — run deterministic offline format and syntax checks.
5. **Syntax** — extract canonical symbols and imports with the pipeline-owned Tree-sitter stage.
6. **Structure** — run **CodeGraphContext** over the exact snapshot.
7. **Semantics** — run **Codebase-Memory** in full mode over the exact snapshot and retain its returned project identifier for later queries.
8. **Retrieval** — build the pipeline's own SQLite FTS5 index and expose exact, full-text, structural, semantic, and hybrid search.
9. **Vector** — build the pipeline-owned LanceDB index over canonical units for semantic retrieval and history-preserving search.
10. **Audit** — verify that all mandatory artifacts and retrieval methods work.
11. **Export** — create repository and report notes in the mounted Obsidian vault.

Stable identifiers are part of the data model:

- unit IDs do not include `run_id`
- symbol IDs do not include `run_id`
- paths are normalized before hashing
- Markdown units are line-accurate and reconstructable from stored source ranges

The LLM profiles supervise and explain. Python, Git, CodeGraphContext, Codebase-Memory, SQLite, and checksums do the data processing.

## Runtime design

Only two long-running containers are used:

- `hermes`: Telegram and Hermes profiles.
- `knowledge-pipeline`: persistent job queue, deterministic stages, indexes, search API, and refresh scheduler.

The Kanban is a human-visible coordination layer. It is **not** the executor. The pipeline database is the source of truth for run state.

## Profiles included

The profile set is configurable in `config/runtime.yaml`; stage ownership is not hardcoded in Python.

- `orchestrator`
- `source-obtainer`
- `data-curator`
- `syntax-analyst`
- `structure-analyst`
- `semantic-analyst`
- `retrieval-manager`
- `quality-auditor`
- `maintainer`
- `knowledge-writer` (disabled by default)

The default Hermes profile is configured as the orchestrator so only one Telegram gateway is required. Other profiles are available for explicit chat, delegated tasks, inspection, retries, and maintenance. Every rendered profile includes the `agent-brain-pipeline` skill and a dependency-free API client for ingest, status, reports, retries, and all five search modes.

## One configuration file

Edit only:

```text
config/runtime.yaml
```

It contains:

- Telegram bot tokens and allowed user IDs.
- GitHub and GitLab access tokens.
- OpenRouter, OpenCode Go, and DeepSeek key pools.
- Same-provider key rotation strategy.
- Cross-provider fallback order.
- Default and per-profile models.
- Enabled profiles and stage ownership.
- Repository refresh interval. Default: **36 hours with up to 12 hours of jitter**, refreshing between 24 and 48 hours.
- Storage and Obsidian paths.

`config/runtime.yaml` is excluded by `.gitignore`. The committed template is `config/runtime.example.yaml`. Bootstrap renders a sanitized `.runtime/pipeline.yaml`, so the pipeline container receives SCM credentials and its internal API token but never receives Telegram or LLM-provider keys.

## Quick start

### 1. Create the local configuration

```bash
cp config/runtime.example.yaml config/runtime.yaml
chmod 600 config/runtime.yaml
```

Fill at least:

- `telegram.bots.orchestrator.bot_token`
- `telegram.bots.orchestrator.allowed_user_ids`
- one provider key and model
- `security.internal_api_token`

### 2. Prepare the Obsidian repository

Clone your separate `agent-brain` repository beside this repository, or change `storage.obsidian_host_path`.

Default layout:

```text
parent/
├── agent-brain-pipeline/
└── agent-brain/
```

### 3. Bootstrap and start

```bash
./scripts/bootstrap.sh
```

This validates the configuration, creates `.runtime/`, renders Hermes profile configuration, builds the pipeline image, and starts both containers.

### 4. Check health

```bash
./scripts/doctor.sh
```

### 5. Ingest from Telegram

Send the orchestrator:

```text
Please ingest https://github.com/NousResearch/hermes-agent
```

You can also use the API directly:

```bash
curl -sS \
  -H "Authorization: Bearer $AGENT_BRAIN_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://github.com/NousResearch/hermes-agent"}' \
  http://localhost:8080/runs
```

## Search methods

### Exact source search

Uses `ripgrep` against the verified extracted snapshot. Best for exact strings, error messages, identifiers, and configuration keys.

### Full-text search

Uses the pipeline SQLite FTS5 index. Best for documentation, headings, file content, APIs, and exact terminology.

### Structural search

Uses CodeGraphContext's graph/search CLI. Best for classes, methods, functions, callers, callees, imports, inheritance, and dependencies.

### Semantic search

Uses Codebase-Memory's bundled semantic query and graph signals. Best for architectural or conceptual questions where the same words may not appear in source.

### Vector search

Uses the pipeline-owned LanceDB index over canonical units. Best for semantic retrieval across documentation and code with deterministic local embeddings.

### Hybrid search

Runs full-text, vector, structural, and semantic retrieval together and returns one evidence package with repository, commit, file, line, method, and score metadata where available.

## Important storage rule

Raw and derived data are separate:

```text
/data/sources/   # immutable raw Git bundles and compressed source snapshots
/data/derived/   # rebuildable analyzer workspaces, CodeGraph, and Codebase-Memory data
/data/runs/      # reports and logs for every run
/data/pipeline.sqlite
```

Never place these databases or raw snapshots in the Obsidian Git repository.

## Key rotation and fallback

Hermes supports credential pools. This repository renders `auth.json` and `credential_pool_strategies` from the central configuration.

Example policy:

```yaml
providers:
  openrouter:
    rotation: round_robin
    keys:
      - sk-or-first
      - sk-or-second

routing:
  primary:
    provider: openrouter
    model: deepseek/deepseek-v4-flash
  fallbacks:
    - provider: opencode-go
      model: deepseek-v4-flash
    - provider: deepseek
      model: deepseek-v4-flash
```

The renderer keeps the first provider key in each profile `.env` and writes additional keys into its Hermes credential pool. The order is:

1. Rotate among healthy keys for the current provider.
2. If that pool is exhausted, move to the first fallback provider.
3. Continue down the fallback chain only when needed.

## Current scope

Version 0.1 intentionally supports only GitHub and GitLab repositories. Websites, PDFs, APIs, papers, and video acquisition can be added later without changing the core artifact model.

## Reproducibility note

The pipeline pins CodeGraphContext and Codebase-Memory versions in `Dockerfile.pipeline`. The Hermes image is configurable because published image tags and digests change; after your first verified deployment, replace the Hermes tag in `config/runtime.yaml` with an immutable image digest.
