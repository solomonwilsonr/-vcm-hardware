import pytest

from vcm_core import VcmError, signing
from vcm_workflow import approve, approve_signed, get_pr, merge_pr, open_pr, render_pr


@pytest.fixture
def feature(sample_repo):
    """main sits at v1.0; `feature` carries v1.1."""
    r = sample_repo
    r.branch("feature", "v1.1")
    r.checkout("v1.0", force=True)
    r._set_ref("heads", "main", r.resolve("v1.0"))
    (r.dir / "HEAD").write_text("ref: refs/heads/main\n")
    return r


def test_merge_fast_forwards_base_and_updates_working_tree(feature):
    r = feature
    pr = open_pr(r, "main", "feature", "Swap LDO", author="bob")
    assert pr["base_ref"] == "main"
    with pytest.raises(VcmError, match="approval"):
        merge_pr(r, pr["id"], "carol")
    with pytest.raises(VcmError, match="own pull request"):
        approve(r, pr["id"], "bob")
    approve(r, pr["id"], "alice")
    merged = merge_pr(r, pr["id"], "carol")
    assert merged["state"] == "merged" and merged["merged_by"] == "carol"
    assert r.branches()["main"] == r.resolve("v1.1")
    assert r.status() == {"added": [], "removed": [], "modified": []}
    assert "merged into main by carol" in render_pr(get_pr(r, pr["id"]))
    with pytest.raises(VcmError, match="merged"):
        merge_pr(r, pr["id"], "carol")


def test_merge_needs_identity_and_required_approvals(feature):
    r = feature
    pr = open_pr(r, "main", "feature", "x", author="bob")
    approve(r, pr["id"], "alice")
    with pytest.raises(VcmError, match="identity"):
        merge_pr(r, pr["id"], "unknown")
    r.save_config({"required_approvals": 2})
    assert get_pr(r, pr["id"])["status"]["blockers"] == ["needs 1 more approval(s)"]
    with pytest.raises(VcmError, match="not mergeable"):
        merge_pr(r, pr["id"], "carol")
    approve(r, pr["id"], "dave")
    assert merge_pr(r, pr["id"], "carol")["state"] == "merged"


def test_merge_refused_when_base_moved(feature):
    r = feature
    pr = open_pr(r, "main", "feature", "x", author="bob")
    approve(r, pr["id"], "alice")
    (r.root / "extra.txt").write_text("1")
    r.commit("main moved", author="alice")
    with pytest.raises(VcmError, match="moved"):
        merge_pr(r, pr["id"], "carol")


def test_merge_refused_against_a_tag_base(sample_repo):
    pr = open_pr(sample_repo, "v1.0", "v1.1", "x", author="bob")
    approve(sample_repo, pr["id"], "alice")
    with pytest.raises(VcmError, match="not the tip of exactly one branch"):
        merge_pr(sample_repo, pr["id"], "carol")


def test_merge_into_protected_branch_is_the_only_way_in(feature):
    r = feature
    r.save_config({"protected_branches": ["main"]})
    (r.root / "extra.txt").write_text("1")
    with pytest.raises(VcmError, match="protected"):
        r.commit("direct", author="alice")
    (r.root / "extra.txt").unlink()
    pr = open_pr(r, "main", "feature", "x", author="bob")
    approve(r, pr["id"], "alice")
    assert merge_pr(r, pr["id"], "carol")["state"] == "merged"


def test_fast_forward_refuses_divergence(feature):
    r = feature
    (r.root / "extra.txt").write_text("1")
    r.commit("main moved", author="alice")
    with pytest.raises(VcmError, match="diverged"):
        r.fast_forward("main", r.branches()["feature"])


def test_signed_approvals_bind_identity_to_a_trusted_key(feature, tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))
    r = feature
    r.save_config({"require_signed_approvals": True})
    pr = open_pr(r, "main", "feature", "x", author="bob")
    approve(r, pr["id"], "alice")  # a typed name no longer counts
    st = get_pr(r, pr["id"])["status"]
    assert st["approvals"] == 0 and "signed" in st["blockers"][0]

    alice = signing.generate_key("alice")
    with pytest.raises(VcmError, match="not trusted"):
        approve_signed(r, pr["id"], "alice")
    signing.trust_key(r.root, "alice", alice["public"])
    got = approve_signed(r, pr["id"], "alice")
    assert got["status"]["approvals"] == 1 and got["status"]["ready_for_merge"]
    assert "alice (signed)" in render_pr(got)

    signing.revoke_key(r.root, alice["key_id"])  # revocation withdraws the approval
    assert get_pr(r, pr["id"])["status"]["approvals"] == 0
    with pytest.raises(VcmError, match="not mergeable"):
        merge_pr(r, pr["id"], "carol")


def test_signed_approval_is_bound_to_the_head_it_approved(feature, tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))
    r = feature
    r.save_config({"require_signed_approvals": True})
    alice = signing.generate_key("alice")
    signing.trust_key(r.root, "alice", alice["public"])
    pr = open_pr(r, "main", "feature", "x", author="bob")
    approve_signed(r, pr["id"], "alice")
    raw = r.load_record("prs", str(pr["id"]))
    raw["head"] = raw["base"]  # someone repoints the PR at different content
    r.save_record("prs", str(pr["id"]), raw)
    assert get_pr(r, pr["id"])["status"]["approvals"] == 0


def test_author_cannot_sign_their_own_approval(feature, tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))
    r = feature
    bob = signing.generate_key("bob")
    signing.trust_key(r.root, "bob", bob["public"])
    pr = open_pr(r, "main", "feature", "x", author="bob")
    with pytest.raises(VcmError, match="own pull request"):
        approve_signed(r, pr["id"], "bob")
