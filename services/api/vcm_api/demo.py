"""A ready-made demo workspace, so a stranger can see VCM working without setting anything up.

The story it tells: a GPU node is upgraded from A100 to H100 NVL (v1.0 -> v1.1), which breaks the
power budget and the driver floor. A teammate now proposes a B200, a part the catalog has never
seen, so the review shows VCM saying what it did and did not check. Someone else holds a lock on the
driver pin, one release is signed, and three units are registered.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from vcm_core import Repository, signing
from vcm_trace import register_device
from vcm_workflow import create_release, open_pr, sign_release

SAMPLE = Path(__file__).resolve().parents[3] / "examples" / "gpu-node"
KEY = "release-bot"  # the dashboard's "Signing key" box is pre-pointed at this name


KEEP = {".vcm", signing.TRUSTDB}  # the trust set belongs to the project, not to a sample revision


def _sync(src: Path, dst: Path) -> None:
    for old in dst.iterdir():
        if old.name not in KEEP:
            shutil.rmtree(old) if old.is_dir() else old.unlink()
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _propose_b200(root: Path) -> None:
    """A tempting but unreviewed change: a part the component catalog has never heard of."""
    manifest = next(root.glob("*.node.json"))
    manifest.write_text(manifest.read_text().replace(
        "NVIDIA H100 NVL 94GB", "NVIDIA B200 SXM 192GB").replace(
        "900-21010-0000-000", "900-74000-0000-000"))


def build_demo(dest: str | Path) -> Repository:
    """Create the demo repository at ``dest`` (an empty or new directory) and return it."""
    if not (SAMPLE / "v1.0").is_dir():
        raise RuntimeError("the demo needs the examples/ folder: run it from a source checkout")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("VCM_KEY_DIR", str(dest / ".vcm" / "demo-keys"))
    repo = Repository.init(dest)
    key = signing.generate_key(KEY)
    signing.trust_key(repo.root, KEY, key["public"])  # shared trust set, committed with the design

    _sync(SAMPLE / "v1.0", dest)
    first = repo.commit("v1.0 gpu-node-01: 8x A100 PCIe 40GB", author="alice")
    repo.tag("v1.0", first.id)
    create_release(repo, "R1.0", "v1.0")
    sign_release(repo, "R1.0", KEY)
    for serial, batch in (("NODE-0001", "RACK-12"), ("NODE-0002", "RACK-12")):
        register_device(repo, serial, "R1.0", batch)

    _sync(SAMPLE / "v1.1", dest)
    second = repo.commit("v1.1 upgrade accelerators to H100 NVL 94GB", author="bob")
    repo.tag("v1.1", second.id)
    create_release(repo, "R1.1", "v1.1")
    register_device(repo, "NODE-0003", "R1.1", "RACK-13")

    repo.branch("b200-eval")
    repo.checkout("b200-eval")
    _propose_b200(dest)
    repo.commit("Evaluate B200 SXM 192GB in place of H100 NVL", author="bob")
    repo.checkout("main", force=True)
    open_pr(repo, "main", "b200-eval", "Evaluate B200 SXM 192GB", author="bob")
    repo.locks.acquire("firmware/driver.conf", "carol", ttl=8 * 3600, note="driver pin for the B200 eval")
    return repo
