"""Drive the real dashboard in a real browser: sign-in, roles, locks, live updates, signatures,
review and merge. It needs Node with Playwright and uvicorn, so it skips quietly where those are
absent (it always runs in the development container); everything it asserts about the API is also
covered by the plain tests."""
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import ROOT, _copy_tree

uvicorn = pytest.importorskip("uvicorn")
pytest.importorskip("fastapi")

NODE = shutil.which("node")
NODE_SAMPLE = ROOT / "examples" / "gpu-node"
SCRIPT = Path(__file__).parent / "ui" / "dashboard.js"
TOKENS = {"alice-token-0123456789": ("alice", "maintainer"), "bob-token-0123456789": "bob",
          "rita-token-0123456789": ("rita", "reader")}


def _have_playwright() -> bool:
    if not NODE:
        return False
    probe = subprocess.run([NODE, "-e", "require('playwright')"], capture_output=True, timeout=60)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _have_playwright(), reason="needs node + playwright")


@pytest.fixture
def served(tmp_path, monkeypatch):
    from vcm_api import create_app
    from vcm_core import Repository, signing
    from vcm_trace import register_device  # noqa: F401  (units are optional for this flow)
    from vcm_workflow import create_release, open_pr, sign_release

    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))
    repo = Repository.init(tmp_path / "hw")
    _copy_tree(NODE_SAMPLE / "v1.0", repo.root)
    info = signing.generate_key("bot")
    signing.trust_key(repo.root, "bot", info["public"])
    repo.commit("v1.0 baseline + trust", author="alice")
    repo.tag("v1.0")
    create_release(repo, "R1", "v1.0")
    sign_release(repo, "R1", "bot")
    create_release(repo, "R2", "v1.0")
    repo.branch("feature")
    repo.checkout("feature")
    _copy_tree(NODE_SAMPLE / "v1.1", repo.root)
    repo.commit("H100 upgrade", author="bob")
    repo.checkout("main", force=True)
    open_pr(repo, "main", "feature", "Upgrade to H100 NVL", author="bob")
    repo.locks.acquire("firmware/driver.conf", "carol", ttl=3600, note="driver bump")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(str(repo.root), tokens=TOKENS),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_dashboard_in_a_real_browser(served):
    run = subprocess.run([NODE, str(SCRIPT), served], capture_output=True, text=True, timeout=240,
                         cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "FAIL" not in run.stdout and "all UI checks passed" in run.stdout
