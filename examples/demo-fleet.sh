#!/usr/bin/env bash
# The shared fleet of record, demonstrated with two independent checkouts.
#
# The point: unit A is registered on one machine and unit B on another, and each machine can then
# answer for the whole fleet — because the registry is in Appwrite TablesDB, not in .vcm. With a
# per-clone registry each would see only its own unit, and a recall would quietly under-report.
#
# Needs Appwrite credentials (see .env.example) and `vcm fleet provision` once.
# Usage: examples/demo-fleet.sh    (set VCM_DEMO_DIR to choose the workspace; it is wiped)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${VCM_DEMO_DIR:-$ROOT/demo-workspace-fleet}"
SAMPLE="$ROOT/examples/gpu-node/v1.0"
export VCM_AUTHOR="${VCM_AUTHOR:-demo}"

if command -v vcm >/dev/null 2>&1; then
  vcm() { command vcm "$@"; }
else
  PYTHONPATH="$(printf '%s:' "$ROOT"/packages/* "$ROOT"/services/*)${PYTHONPATH:-}"
  export PYTHONPATH
  vcm() { python3 -m vcm_cli "$@"; }
fi

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }

# Credentials are read from .env by walking up from the working directory, and the demo checkouts
# live outside the repository, so load them into the environment explicitly.
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi

if [ -z "${VCM_APPWRITE_API_KEY:-}" ]; then
  echo "This demo needs Appwrite credentials. cp .env.example .env and fill it in." >&2
  exit 1
fi

step "0. Is the fleet of record reachable?"
vcm fleet health

# Two serials per run, so repeated runs do not collide with units already in the fleet.
TAG="$(date +%H%M%S)"
A="DEMO-A$TAG"
B="DEMO-B$TAG"

rm -rf "$WORK"; mkdir -p "$WORK/alice" "$WORK/bob"

# Alice and Bob commit independently, so their revisions differ exactly as they would in life.
for who in alice bob; do
  cp -R "$SAMPLE"/. "$WORK/$who"/
  ( cd "$WORK/$who"
    vcm init . >/dev/null
    VCM_AUTHOR="$who" vcm commit -m "v1.0 as $who" --tag v1.0 >/dev/null
    vcm release create R1 --rev v1.0 >/dev/null )
done

step "1. Alice registers $A on her machine"
( cd "$WORK/alice" && vcm device register "$A" --release R1 )

step "2. Bob registers $B on his"
( cd "$WORK/bob" && vcm device register "$B" --release R1 )

step "3. Bob's local cache holds only his own unit"
ls "$WORK/bob/.vcm/devices/" | sed 's/^/  /'

step "4. ...but Bob can answer for the whole fleet"
( cd "$WORK/bob" && vcm device list )

step "5. A duplicate serial is refused across machines"
( cd "$WORK/alice" && vcm device register "$B" --release R1 ) || true

step "6. A recall names what this checkout cannot describe, rather than dropping it"
( cd "$WORK/bob" && vcm fleet recall --model "A100" )

printf '\nThe registry is shared; design history is not. Bob reports Alice'"'"'s unit as UNRESOLVED\n'
printf 'instead of silently answering for a partial fleet.\n'
