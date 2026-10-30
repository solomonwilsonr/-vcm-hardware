#!/usr/bin/env bash
# End-to-end VCM demo: commit two board revisions, then diff, impact, validate, release, trace.
# Usage: examples/demo.sh   (set VCM_DEMO_DIR to choose the workspace; it is wiped on each run)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${VCM_DEMO_DIR:-$ROOT/demo-workspace}"
SAMPLE="$ROOT/examples/sample-pcb"
export VCM_AUTHOR="${VCM_AUTHOR:-demo}"

if command -v vcm >/dev/null 2>&1; then
  vcm() { command vcm "$@"; }
else
  PYTHONPATH="$(printf '%s:' "$ROOT"/packages/* "$ROOT"/services/*)${PYTHONPATH:-}"
  export PYTHONPATH
  vcm() { python3 -m vcm_cli "$@"; }
fi

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
sync_tree() { find "$WORK" -mindepth 1 -maxdepth 1 ! -name .vcm -exec rm -rf {} +; cp -R "$1"/. "$WORK"/; }

rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"

step "1. Initialise a repository and commit v1.0"
vcm init .
sync_tree "$SAMPLE/v1.0"
vcm commit -m "v1.0 sensor node: AMS1117-3.3 LDO" --author alice --tag v1.0

step "2. Commit v1.1 (LDO swapped, output capacitor changed)"
sync_tree "$SAMPLE/v1.1"
vcm commit -m "v1.1 swap to MCP1700 LDO, ceramic output cap" --author bob --tag v1.1
vcm log

step "3. What exactly changed? (semantic diff)"
vcm diff v1.0 v1.1

step "4. What else could that change affect? (graph query and impact report)"
vcm graph query --component U1 --rev v1.1
vcm impact v1.0 v1.1

step "5. Validation (built-in checks; kicad-cli is used only if installed)"
vcm bom validate --rev v1.1
vcm validate --rev v1.0
vcm validate --rev v1.1 --kicad --compare v1.0

step "6. Release manifests and reproducibility check"
vcm release create R1.0 --rev v1.0
vcm release create R1.1 --rev v1.1 --out "$WORK/.vcm/R1.1.manifest.json"
vcm release verify R1.0
vcm release verify R1.1

step "7. Physical devices: register, QR, passport"
vcm device register SN-0001 --release R1.0 --batch BATCH-A
vcm device register SN-0002 --release R1.0 --batch BATCH-A
vcm device register SN-0003 --release R1.1 --batch BATCH-B
vcm device passport SN-0003 --no-svg | sed -n '1,40p'

step "8. Which devices are affected?"
vcm device affected --component U1 --value AMS1117-3.3
vcm device affected --revision v1.1

step "9. Rollback plan v1.1 -> v1.0 (what is software-reversible?)"
vcm rollback plan v1.0 --from v1.1

step "10. Hardware pull request and time machine"
vcm pr open --base v1.0 --head v1.1 --title "Swap LDO to MCP1700" --author bob
vcm pr approve 1 --by alice
vcm timeline

step "11. Explain the change end to end (first 30 lines)"
vcm explain v1.0 v1.1 | sed -n '1,30p'

step "Tamper check: corrupt a pinned blob and re-verify (expected to FAIL)"
HASH="$(sha256sum firmware/main.c | cut -d' ' -f1)"
BLOB=".vcm/objects/${HASH:0:2}/${HASH:2}"
chmod u+w "$BLOB" && printf 'tampered' >> "$BLOB"
if vcm release verify R1.1; then echo "ERROR: tampering was not detected" >&2; exit 1; else echo "(tampering detected as expected)"; fi
printf '\nDemo complete. Workspace: %s\n' "$WORK"
