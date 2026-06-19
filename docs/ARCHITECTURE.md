# Architecture

## Execution ownership

- **Hermes profile:** decides, submits, inspects, explains, retries.
- **Pipeline worker:** executes deterministic stages.
- **Kanban:** mirrors work state for humans and agents.
- **SQLite:** authoritative run/stage state.

## Ingestion chain

```text
Telegram -> orchestrator -> POST /runs -> SQLite queue -> pipeline worker
  -> acquire -> integrity -> normalize -> lint -> CodeGraphContext -> Codebase-Memory
  -> FTS/exact retrieval -> audit -> Obsidian export
```

## Why CodeGraphContext and Codebase-Memory are both mandatory

CodeGraphContext is used as the explicit structural graph stage. Codebase-Memory is used as the code knowledge/semantic stage and provides bundled semantic search, graph search, and code-oriented relationships. Their original outputs remain separate and are tied to the same exact commit. Hybrid retrieval queries both rather than forcing them into one proprietary database schema.

## Raw versus derived

Raw source artifacts are immutable and checksum-addressed. All curation, graphs, indexes, reports, and Obsidian notes are derived and can be rebuilt from the raw snapshot and exact tool versions.

The first three deterministic stages are intentionally split:

- Integrity checks the raw acquisition artifacts and snapshot safety.
- Normalize catalogs files and units into canonical JSONL and SQLite rows.
- Lint performs offline syntax and link validation only.
