# Architecture

## Execution ownership

- **Hermes profile:** decides, submits, inspects, explains, retries.
- **Pipeline worker:** executes deterministic stages.
- **Kanban:** mirrors work state for humans and agents.
- **SQLite:** authoritative run/stage state.

## Ingestion chain

```text
Telegram -> orchestrator -> POST /runs -> SQLite queue -> pipeline worker
  -> acquire -> integrity -> normalize -> lint -> syntax -> CodeGraphContext -> Codebase-Memory
  -> FTS/exact retrieval -> audit -> Obsidian export
```

## Why CodeGraphContext and Codebase-Memory are both mandatory

CodeGraphContext is used as the explicit structural graph stage. Codebase-Memory is used as the code knowledge/semantic stage and provides bundled semantic search, graph search, and code-oriented relationships. Their original outputs remain separate and are tied to the same exact commit. Hybrid retrieval queries both rather than forcing them into one proprietary database schema.

## Raw versus derived

Raw source artifacts are immutable and checksum-addressed. All curation, graphs, indexes, reports, and Obsidian notes are derived and can be rebuilt from the raw snapshot and exact tool versions.

## Canonical identities and provenance

The pipeline stores canonical records with deterministic identities so repeated runs over the same source commit produce stable database keys.

- Unit IDs are derived from `source_id`, `commit_sha`, normalized path, unit type, start line or byte, end line or byte, and content SHA-256.
- Symbol IDs are derived from `source_id`, `commit_sha`, normalized path, language, symbol kind, qualified name, start byte, end byte, and content SHA-256.
- `run_id` is lineage only. It records which execution wrote a row, but it is not part of the identity hash.
- Markdown units are line-accurate and reconstructable from their recorded source line ranges.
- SQLite migrations are additive and preserve existing runs, files, units, and reports.
- Tree-sitter syntax extraction is the canonical source for symbols and imports when parsing succeeds; fallback fixed-line chunks are reserved for parse failures, unsupported languages, and oversized symbols.
