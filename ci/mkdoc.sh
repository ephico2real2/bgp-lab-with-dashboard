#!/usr/bin/env bash
# Render docs/CI-EVIDENCE.md from a run's own output. Run by a human after a
# green lab-ci job — CI does not commit this file (that loop nobody wants).
# The workflow prints this command with the values filled in.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh disable=SC1091
. ./lib.sh
cd "$ROOT"

run_url=""
commit=""
digest=""
check_file=""
timings_file=""
shot_base=""
out="$ROOT/docs/CI-EVIDENCE.md"

usage() {
  echo "usage: ci/mkdoc.sh --run-url URL --commit SHA --digest DIGEST \\" >&2
  echo "         --check-file ci/out/check.txt --timings-file ci/out/walk.log \\" >&2
  echo "         --shot-base RAW_URL_BASE [--out docs/CI-EVIDENCE.md]" >&2
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --run-url) run_url=${2:-}; shift 2 ;;
    --commit) commit=${2:-}; shift 2 ;;
    --digest) digest=${2:-}; shift 2 ;;
    --check-file) check_file=${2:-}; shift 2 ;;
    --timings-file) timings_file=${2:-}; shift 2 ;;
    --shot-base) shot_base=${2:-}; shift 2 ;;
    --out) out=${2:-}; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

[ -n "$run_url" ] && [ -n "$commit" ] || usage

read_or_missing() {
  local path=$1
  if [ -z "$path" ]; then
    echo "(not given)"
    return
  fi
  if [ ! -f "$path" ]; then
    echo "(file not found: ${path})"
    return
  fi
  cat "$path"
}

check_body=$(read_or_missing "$check_file")
timings_body=$(read_or_missing "$timings_file")
shot1="${shot_base:+$shot_base/01-steady.png}"
shot2="${shot_base:+$shot_base/02-clear-bgp.png}"
shot3="${shot_base:+$shot_base/03-recovered.png}"
digest_line=${digest:-"(not pushed)"}

mkdir -p "$(dirname "$out")"
cat > "$out" <<EOF
# CI evidence

Four FRR routers — two companies multi-homed to two ISPs — plus the live
dashboard. The topology is the one in the README: companyA and companyB each
peer with ISP1 and ISP2; the ISPs peer with each other and transit
\`10.1.1.0/24\` (companyA) and \`192.168.1.0/24\` (companyB).

## Bring-up (no containerlab)

\`\`\`bash
docker compose -f compose/docker-compose.yml up -d --wait
# or: ci/up.sh
\`\`\`

Dashboard: http://127.0.0.1:8088

## Screenshots

### 01-steady

The dashboard at rest: four routers, eBGP sessions Established.

$(if [ -n "$shot1" ]; then echo "![01-steady](${shot1})"; else echo "(no screenshot URL)"; fi)

### 02-clear-bgp

After \`clear bgp *\` on isp1 — the blog's own demo. Sessions toward isp1 are
not Established; the graph shows the drop.

$(if [ -n "$shot2" ]; then echo "![02-clear-bgp](${shot2})"; else echo "(no screenshot URL)"; fi)

### 03-recovered

Sessions Established again after isp1's peers come back.

$(if [ -n "$shot3" ]; then echo "![03-recovered](${shot3})"; else echo "(no screenshot URL)"; fi)

## Check

\`\`\`
${check_body}
\`\`\`

## Timings

\`\`\`
${timings_body}
\`\`\`

## How this was produced

- run: ${run_url}
- commit: \`${commit}\`
- dashboard image digest: \`${digest_line}\`
- rendered by \`ci/mkdoc.sh\` from that run's check table, timings and screenshot URLs
EOF

echo "wrote ${out}"
