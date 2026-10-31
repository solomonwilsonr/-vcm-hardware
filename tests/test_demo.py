"""The one-command demo must always build, and must tell the story the README promises."""
import pytest

from vcm_ai import analyze
from vcm_api.demo import build_demo
from vcm_core import signing
from vcm_workflow import get_pr, verify_release


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    import os
    base = tmp_path_factory.mktemp("demo")
    old = os.environ.get("VCM_KEY_DIR")
    os.environ["VCM_KEY_DIR"] = str(base / "keys")
    try:
        yield build_demo(base / "ws")
    finally:
        if old is None:
            os.environ.pop("VCM_KEY_DIR")
        else:
            os.environ["VCM_KEY_DIR"] = old


def test_demo_history_and_headline_findings(demo):
    assert set(demo.tags()) == {"v1.0", "v1.1"}
    assert demo.current_branch == "main" and demo.status() == {
        "added": [], "removed": [], "modified": []}
    rep = analyze(demo, "v1.0", "v1.1")
    titles = " ".join(f["title"] for f in rep["findings"])
    assert "PSU_BANK" in titles and "driver" in titles.lower()  # the payoff the README leads with
    assert {f["severity"] for f in rep["findings"]} >= {"critical"}


def test_demo_has_signed_release_units_lock_and_open_pr(demo):
    r10 = verify_release(demo, "R1.0", require_signature=True)
    assert r10["ok"] and r10["authenticated"]
    assert not verify_release(demo, "R1.1")["authenticated"]  # unsigned, and said so
    assert [d["serial"] for d in demo.list_records("devices")] == [
        "NODE-0001", "NODE-0002", "NODE-0003"]
    assert [(lk["path"], lk["holder"]) for lk in demo.locks.list()] == [
        ("firmware/driver.conf", "carol")]
    pr = get_pr(demo, 1)
    assert pr["state"] == "open" and pr["author"] == "bob" and pr["title"].startswith("Evaluate B200")
    titles = " ".join(f["title"] for f in pr["impact"]["findings"])
    assert "not in the component catalog" in titles  # the honesty feature, visible in the review
    assert pr["status"]["critical_findings"]
    assert pr["status"]["blockers"] == ["needs 1 more approval(s)"]
    assert pr["base_ref"] == "main"
    assert signing.load_trustdb(demo.root)["keys"]  # the signing key is trusted in the repo
