# Central configuration

Only `config/runtime.yaml` is edited by the operator. It is excluded from Git and should remain mode `0600`.

## Provider keys

Each provider has an ordered `keys` list and a Hermes-supported rotation strategy:

- `fill_first`
- `round_robin`
- `least_used`
- `random`

The renderer places the first key in the profile `.env` so Hermes auto-discovers it, then places additional keys in that profile's `auth.json` credential pool. Cross-provider failover is rendered from `routing.fallbacks` into the top-level Hermes `fallback_providers` list.

A provider is ignored when `enabled: false`. Enabling it requires a non-empty key and a concrete model ID in any active fallback/profile override.

## Telegram

`telegram.bots.orchestrator` controls the default Hermes gateway. Add another entry only when another profile needs its own independent Telegram bot token.

## Wiki

The knowledge-writer profile is enabled by default and uses the mounted Obsidian vault at `/vault`.
The wiki paths are configured under `wiki:` in `config/runtime.yaml`:

- `vault_path`: the mounted vault root
- `raw_path`: immutable raw evidence
- `wiki_path`: the working wiki tree
- `candidate_path`: candidate pages written by the knowledge-writer
- `canonical_path`: promoted canonical pages
- `auto_generate_after_audit`: whether a ready-for-wiki run should trigger wiki generation
- `auto_promote`: kept false unless you intentionally want the agent to promote its own work

## Refresh schedule

The default uses `refresh_interval_hours: 36` and `refresh_jitter_hours: 12`, causing each source's next fetch to be scheduled randomly between 24 and 48 hours after acquisition. The scheduler polls every ten minutes.

## Profiles and stage ownership

`profiles` determines which profile directories are rendered. `stages.<stage>.owner_profile` controls which profile is shown as responsible in run state and Kanban views. The deterministic worker still executes the code; the profile supervises, explains, retries, and handles user interaction.

For the current pipeline split, `integrity`, `normalize`, and `lint` remain owned by `data-curator`, while `syntax` is owned by `syntax-analyst` and runs before `structure`.
