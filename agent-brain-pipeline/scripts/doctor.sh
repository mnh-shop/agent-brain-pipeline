#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .runtime/compose.env ]]; then
  echo "Missing .runtime/compose.env. Run ./scripts/bootstrap.sh first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .runtime/compose.env
set +a

printf '%-28s' 'Pipeline container:'
docker compose --env-file .runtime/compose.env ps --status running knowledge-pipeline --quiet | grep -q . && echo PASS || echo FAIL

printf '%-28s' 'Hermes container:'
docker compose --env-file .runtime/compose.env ps --status running hermes --quiet | grep -q . && echo PASS || echo FAIL

printf '%-28s' 'Pipeline health:'
curl -fsS "http://localhost:${PIPELINE_PORT}/health" >/dev/null && echo PASS || echo FAIL

printf '%-28s' 'CodeGraphContext:'
docker compose --env-file .runtime/compose.env exec -T knowledge-pipeline codegraphcontext --help >/dev/null && echo PASS || echo FAIL

printf '%-28s' 'Codebase-Memory:'
docker compose --env-file .runtime/compose.env exec -T knowledge-pipeline codebase-memory-mcp --help >/dev/null && echo PASS || echo FAIL

printf '%-28s' 'Configuration:'
python3 scripts/render_runtime.py --config config/runtime.yaml --root "$ROOT" --validate-only >/dev/null && echo PASS || echo FAIL
