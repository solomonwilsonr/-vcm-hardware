#!/usr/bin/env bash
# One command to get VCM running on a real Appwrite project, then show the shared fleet.
#
#   cp .env.example .env     # fill in endpoint, project, API key, database
#   make hackathon           # or: bash examples/hackathon-setup.sh
#
# Safe to re-run: provisioning is idempotent and graph rows use deterministic ids.
# It stops at the first problem and says what to fix.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if command -v vcm >/dev/null 2>&1; then
  vcm() { command vcm "$@"; }
else
  PYTHONPATH="$(printf '%s:' "$ROOT"/packages/* "$ROOT"/services/*)${PYTHONPATH:-}"
  export PYTHONPATH
  vcm() { python3 -m vcm_cli "$@"; }
fi
step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
die() { printf '\n\033[31mStopped:\033[0m %s\n' "$*" >&2; exit 1; }

step "1. Credentials"
[ -f .env ] || die "no .env. Run: cp .env.example .env  and fill it in (see the comments inside)."
set -a; . ./.env; set +a
for v in VCM_APPWRITE_ENDPOINT VCM_APPWRITE_PROJECT VCM_APPWRITE_API_KEY VCM_APPWRITE_DATABASE; do
  [ -n "${!v:-}" ] || die "$v is empty in .env"
done
echo "endpoint: $VCM_APPWRITE_ENDPOINT  project: $VCM_APPWRITE_PROJECT  database: $VCM_APPWRITE_DATABASE"

step "2. Is the project reachable?"
vcm fleet health || die "cannot reach Appwrite. Check the endpoint region, project id and API key scopes."

step "3. Create the tables and indexes (idempotent)"
vcm fleet provision
vcm graph provision

step "4. Push the sample node's dependency graph into TablesDB"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
cp -R examples/gpu-node/v1.1/. "$W"/
( cd "$W" && vcm init . >/dev/null && VCM_AUTHOR=demo vcm commit -m "v1.1" --tag v1.1 >/dev/null \
    && vcm graph push --rev v1.1 && vcm graph query --component GPU0 --rev v1.1 --backend appwrite )

step "5. Two checkouts, one shared fleet"
bash examples/demo-fleet.sh

step "Done"
echo "Open the Appwrite console -> Databases -> $VCM_APPWRITE_DATABASE and screenshot:"
echo "  vcm_devices  (the units both checkouts registered)"
echo "  vcm_nodes / vcm_edges  (the dependency graph)"
echo "Then record the demo using docs/hackathon.md, and submit."
