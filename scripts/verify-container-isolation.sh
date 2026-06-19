#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

if [[ ! -f .runtime/compose.env ]]; then
  fail "missing .runtime/compose.env"
fi

set -a
# shellcheck disable=SC1091
source .runtime/compose.env
set +a

services="$(docker compose --env-file .runtime/compose.env config --services)"
service_count="$(printf '%s\n' "$services" | sed '/^$/d' | wc -l | tr -d ' ')"
[[ "$service_count" -eq 2 ]] || fail "expected exactly two compose services"
printf '%s\n' "$services" | grep -Fxq knowledge-pipeline || fail "missing knowledge-pipeline service"
printf '%s\n' "$services" | grep -Fxq hermes || fail "missing hermes service"

pipeline_running="$(docker compose --env-file .runtime/compose.env ps --status running --quiet knowledge-pipeline | tr -d '[:space:]')"
hermes_running="$(docker compose --env-file .runtime/compose.env ps --status running --quiet hermes | tr -d '[:space:]')"
[[ -n "$pipeline_running" ]] || fail "knowledge-pipeline is not running"
[[ -n "$hermes_running" ]] || fail "hermes is not running"

pipeline_mounts="$(docker inspect agent-brain-pipeline --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}')"
hermes_mounts="$(docker inspect agent-brain-hermes --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}')"

printf '%s\n' "$pipeline_mounts" | grep -Fq "${DATA_HOST_PATH} -> /data" || fail "pipeline /data mount missing"
printf '%s\n' "$pipeline_mounts" | grep -Fq "${OBSIDIAN_HOST_PATH} -> /vault" || fail "pipeline /vault mount missing"
printf '%s\n' "$hermes_mounts" | grep -Fq "/opt/data" || fail "hermes persistent runtime volume missing"
printf '%s\n' "$hermes_mounts" | grep -Fq "${OBSIDIAN_HOST_PATH} -> /vault" || fail "hermes /vault mount missing"
printf '%s\n' "$hermes_mounts" | grep -Fq ' -> /data' && fail "hermes must not mount /data"
printf '%s\n' "$pipeline_mounts" | grep -Fq '/.hermes' && fail "pipeline must not mount host ~/.hermes"
printf '%s\n' "$hermes_mounts" | grep -Fq '/.hermes' && fail "hermes must not mount host ~/.hermes"

docker compose --env-file .runtime/compose.env exec -T knowledge-pipeline sh -lc 'test -n "$AGENT_BRAIN_API_TOKEN"' >/dev/null || fail "pipeline token missing"
docker compose --env-file .runtime/compose.env exec -T hermes sh -lc 'test -n "$AGENT_BRAIN_API_TOKEN"' >/dev/null || fail "hermes token missing"

docker compose --env-file .runtime/compose.env exec -T hermes sh -lc 'curl -fsS http://knowledge-pipeline:8080/health >/dev/null' >/dev/null || fail "hermes cannot reach pipeline health"

docker compose --env-file .runtime/compose.env exec -T knowledge-pipeline sh -lc 'tmp=$(mktemp /data/isolation.XXXXXX) && echo ok > "$tmp" && test -f "$tmp" && rm -f "$tmp"' >/dev/null || fail "pipeline cannot write /data"
docker compose --env-file .runtime/compose.env exec -T knowledge-pipeline sh -lc 'tmp=$(mktemp /vault/isolation.XXXXXX) && echo ok > "$tmp" && test -f "$tmp" && rm -f "$tmp"' >/dev/null || fail "pipeline cannot write /vault"

vault_probe="isolation-vault-probe-$$.txt"
docker compose --env-file .runtime/compose.env exec -T knowledge-pipeline sh -lc "echo hermes-check > /vault/${vault_probe}" >/dev/null || fail "pipeline could not create vault probe"
docker compose --env-file .runtime/compose.env exec -T hermes sh -lc "test -r /vault/${vault_probe} && cat /vault/${vault_probe} >/dev/null" >/dev/null || fail "hermes could not read vault probe"
docker compose --env-file .runtime/compose.env exec -T knowledge-pipeline sh -lc "rm -f /vault/${vault_probe}" >/dev/null || fail "pipeline could not remove vault probe"

echo "PASS: container isolation verified"
