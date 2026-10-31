#!/usr/bin/env bash
# 60-second cut of examples/demo.sh. Beats mirror docs/demo-script.md.
# Usage: examples/demo-60s.sh   (PAUSE=1 waits for Enter between beats; VCM_DEMO_DIR sets workspace)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${VCM_DEMO_DIR:-$ROOT/demo-workspace-60s}"
SAMPLE="$ROOT/examples/gpu-node"
export VCM_AUTHOR="${VCM_AUTHOR:-demo}"

if command -v vcm >/dev/null 2>&1; then
  vcm() { command vcm "$@"; }
else
  PYTHONPATH="$(printf '%s:' "$ROOT"/packages/* "$ROOT"/services/*)${PYTHONPATH:-}"
  export PYTHONPATH
  vcm() { python3 -m vcm_cli "$@"; }
fi

beat() {
  printf '\n\033[1m=== %s ===\033[0m\n' "$*"
  if [ -n "${PAUSE:-}" ]; then read -r _; fi
}
sync_tree() { find "$WORK" -mindepth 1 -maxdepth 1 ! -name .vcm -exec rm -rf {} +; cp -R "$1"/. "$WORK"/; }

rm -rf "$WORK"
mkdir -p "$WORK"
cd "$WORK"

# Setup (not part of the 60 s): two commits, a release each, three registered nodes.
vcm init . >/dev/null
sync_tree "$SAMPLE/v1.0"
vcm commit -m "v1.0 gpu-node-01: 8x A100 PCIe 40GB" --author alice --tag v1.0 >/dev/null
sync_tree "$SAMPLE/v1.1"
vcm commit -m "v1.1 upgrade accelerators to H100 NVL 94GB" --author bob --tag v1.1 >/dev/null
vcm release create R1.0 --rev v1.0 >/dev/null
vcm release create R1.1 --rev v1.1 >/dev/null
vcm device register NODE-0001 --release R1.0 --batch RACK-12 >/dev/null
vcm device register NODE-0002 --release R1.0 --batch RACK-12 >/dev/null
vcm device register NODE-0003 --release R1.1 --batch RACK-13 >/dev/null

beat "0:00 Two committed configurations"
vcm log

beat "0:08 What changed? (semantic diff, grouped)"
vcm diff v1.0 v1.1

beat "0:20 What else is affected? (every finding carries a confidence class)"
vcm impact v1.0 v1.1 | sed -n '1,36p'  # verified + first rule-based findings

beat "0:38 Which physical units are affected?"
vcm device affected --component GPU0 --value "NVIDIA A100 PCIe 40GB"

beat "0:50 Verify the release, then tamper and verify again (expected to FAIL)"
vcm release verify R1.1
HASH="$(sha256sum firmware/driver.conf | cut -d' ' -f1)"
BLOB=".vcm/objects/${HASH:0:2}/${HASH:2}"
chmod u+w "$BLOB" && printf 'tampered' >> "$BLOB"
if vcm release verify R1.1; then echo "ERROR: tampering was not detected" >&2; exit 1; else echo "(tampering detected as expected)"; fi
