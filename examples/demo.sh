#!/usr/bin/env bash
# End-to-end VCM demo on a GPU node: commit two fleet configurations, then diff, impact,
# validate, release, and trace which physical units are affected.
# Usage: examples/demo.sh   (set VCM_DEMO_DIR to choose the workspace; it is wiped on each run)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${VCM_DEMO_DIR:-$ROOT/demo-workspace}"
SAMPLE="$ROOT/examples/gpu-node"
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

step "1. Initialise a repository and commit the current fleet configuration"
vcm init .
sync_tree "$SAMPLE/v1.0"
vcm commit -m "v1.0 gpu-node-01: 8x A100 PCIe 40GB" --author alice --tag v1.0

step "2. Commit the proposed upgrade (A100 -> H100 NVL, same chassis and PSUs)"
sync_tree "$SAMPLE/v1.1"
vcm commit -m "v1.1 upgrade accelerators to H100 NVL 94GB" --author bob --tag v1.1
vcm log

step "3. What exactly changed? (semantic diff of the node inventory)"
vcm diff v1.0 v1.1

step "4. What does the component catalog actually know about this node?"
vcm catalog coverage --rev v1.1

step "5. What else could that change affect? (graph query and impact report)"
vcm graph query --component GPU0 --rev v1.1
vcm impact v1.0 v1.1

step "6. Validation (built-in checks on the committed configuration)"
vcm validate --rev v1.0
vcm validate --rev v1.1 --compare v1.0

step "7. Release manifests and reproducibility check"
vcm release create R1.0 --rev v1.0
vcm release create R1.1 --rev v1.1 --out "$WORK/.vcm/R1.1.manifest.json"
vcm release verify R1.0
vcm release verify R1.1

step "8. Physical units: register nodes against a release, with QR and passport"
vcm device register NODE-0001 --release R1.0 --batch RACK-12
vcm device register NODE-0002 --release R1.0 --batch RACK-12
vcm device register NODE-0003 --release R1.1 --batch RACK-13
vcm device passport NODE-0003 --no-svg | sed -n '1,40p'

step "9. Which physical units are affected? (the fleet question)"
vcm device affected --component GPU0 --value "NVIDIA A100 PCIe 40GB"
vcm device affected --revision v1.1

step "10. Rollback plan v1.1 -> v1.0 (what is reversible without a site visit?)"
vcm rollback plan v1.0 --from v1.1

step "11. Change review and time machine"
vcm pr open --base v1.0 --head v1.1 --title "Upgrade gpu-node-01 to H100 NVL" --author bob
vcm pr approve 1 --by alice
vcm timeline

step "12. Explain the change end to end (first 30 lines)"
vcm explain v1.0 v1.1 | sed -n '1,30p'

step "11b. Sign the release and verify authenticity (Ed25519, offline)"
export VCM_KEY_DIR="$WORK/.vcm/demo-keys"
vcm key gen release-bot --trust
vcm release sign R1.1 --key release-bot
vcm release verify R1.1 --require-signature
# The trust set is a normal file: commit it so everyone who clones shares it.
vcm commit -m "trust release-bot signing key" --author alice

step "11c. Share through a remote: push, clone elsewhere, verify the clone"
SHARE="$(mktemp -d)"
trap 'rm -rf "$SHARE"' EXIT
vcm remote add origin "$SHARE/remote"
vcm push origin
vcm clone "$SHARE/remote" "$SHARE/clone"
(cd "$SHARE/clone" && vcm release verify R1.1 && vcm log)

step "11d. Lock a file instead of merging it"
vcm lock firmware/driver.conf --holder alice --note "driver bump"
vcm locks
vcm unlock firmware/driver.conf --holder alice

step "Tamper check: corrupt a pinned blob and re-verify (expected to FAIL)"
HASH="$(sha256sum firmware/driver.conf | cut -d' ' -f1)"
BLOB=".vcm/objects/${HASH:0:2}/${HASH:2}"
chmod u+w "$BLOB" && printf 'tampered' >> "$BLOB"
if vcm release verify R1.1; then echo "ERROR: tampering was not detected" >&2; exit 1; else echo "(tampering detected as expected)"; fi
printf '\nDemo complete. Workspace: %s\n' "$WORK"
