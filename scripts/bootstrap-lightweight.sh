#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CONFIG="${AGENT_BRAIN_CONFIG:-$ROOT/config/runtime.yaml}"
if [[ ! -f "$CONFIG" ]]; then
  cp "$ROOT/config/runtime.example.yaml" "$ROOT/config/runtime.yaml"
  chmod 600 "$ROOT/config/runtime.yaml"
  echo "Created config/runtime.yaml. Fill the placeholders, then rerun bootstrap-lightweight." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker with the Compose plugin is required." >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import yaml' >/dev/null 2>&1; then
  "$PYTHON" -m venv "$ROOT/.runtime/bootstrap-venv"
  "$ROOT/.runtime/bootstrap-venv/bin/pip" install --quiet "PyYAML==6.0.2"
  PYTHON="$ROOT/.runtime/bootstrap-venv/bin/python"
fi

"$PYTHON" "$ROOT/scripts/render_runtime.py" --config "$CONFIG" --root "$ROOT"

set -a
# shellcheck disable=SC1091
source "$ROOT/.runtime/compose.env"
set +a

mkdir -p "$DATA_HOST_PATH" "$HERMES_HOST_PATH" "$OBSIDIAN_HOST_PATH"

docker compose \
  -f "$ROOT/compose.yaml" \
  -f "$ROOT/compose.lightweight.yaml" \
  --env-file "$ROOT/.runtime/compose.env" \
  build knowledge-pipeline hermes

docker compose \
  -f "$ROOT/compose.yaml" \
  -f "$ROOT/compose.lightweight.yaml" \
  --env-file "$ROOT/.runtime/compose.env" \
  up -d

echo
echo "Agent Brain Pipeline lightweight stack is starting."
echo "Pipeline API: http://localhost:${PIPELINE_PORT}"
echo "Hermes image: ${HERMES_IMAGE:-agent-brain-hermes:lite}"
