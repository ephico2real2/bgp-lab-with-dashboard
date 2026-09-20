# Shared by ci/up.sh, ci/check.sh, ci/shots.sh, ci/down.sh.
# shellcheck shell=bash

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT/compose/docker-compose.yml}"
BGP_PY="$ROOT/ci/bgp.py"
LAB_PREFIX="${LAB_PREFIX:-clab-simple-lab}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8089}"
export DASHBOARD_PORT   # compose reads it for the published port
DASHBOARD_URL="${DASHBOARD_URL:-http://127.0.0.1:${DASHBOARD_PORT}}"

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

container_name() {
  printf '%s-%s' "$LAB_PREFIX" "$1"
}

# Nodes from the topology YAML (skip dashboard). No hardcoded list.
lab_nodes() {
  python3 "$BGP_PY" nodes --topology "$ROOT/simple.clab.yml"
}

vtysh_json() {
  local name=$1
  shift
  docker exec "$name" vtysh -c "$*"
}
