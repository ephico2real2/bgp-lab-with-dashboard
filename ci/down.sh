#!/usr/bin/env bash
# Tear the compose lab down. --rmi is optional (local images only).
# Reports what compose actually removed; no unconditional claims.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh disable=SC1091
. ./lib.sh
cd "$ROOT"

args=(down --remove-orphans)
if [ "${1:-}" = "--rmi" ]; then
  args+=(--rmi local)
fi

if ! compose ps -a --format '{{.Name}}' >/dev/null 2>&1; then
  echo "compose ps failed; nothing to report"
  exit 1
fi

before=$(compose ps -a --format '{{.Name}} {{.State}}' 2>&1 || true)
images_before=""
if [ "${1:-}" = "--rmi" ]; then
  images_before=$(compose images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>&1 || true)
fi

out=""
rc=0
out=$(compose "${args[@]}" 2>&1) || rc=$?
printf '%s\n' "$out"

echo
echo "-- what was actually removed --"
removed=0
while IFS= read -r line; do
  case "$line" in
    *Removed*|*removed*)
      echo "  $line"
      removed=$((removed + 1))
      ;;
  esac
done < <(printf '%s\n' "$out")

if [ "$removed" -eq 0 ]; then
  echo "  compose printed no Removed lines (rc=${rc})"
  echo "  before: $(printf '%s' "$before" | tr '\n' ';')"
fi

if [ "${1:-}" = "--rmi" ]; then
  echo "  --rmi local requested; images before: $(printf '%s' "$images_before" | tr '\n' ';')"
fi

exit "$rc"
