import pytest

from vcm_core import Repository, VcmError, remote, signing
from vcm_trace import register_device
from vcm_workflow import create_release, sign_release


@pytest.fixture
def origin(tmp_path):
    return tmp_path / "origin"


def test_push_then_clone_reproduces_history_and_files(sample_repo, origin):
    create_release(sample_repo, "R1.0", "v1.0")
    register_device(sample_repo, "SN-1", "R1.0")
    res = remote.push(sample_repo, str(origin), holder="alice")
    assert res["branches"]["main"].startswith("updated") and set(res["tags"]) == {"v1.0", "v1.1"}
    assert res["records"] == 2

    clone = remote.clone(str(origin), origin.parent / "clone")
    assert clone.head_id() == sample_repo.head_id()
    assert clone.tags() == sample_repo.tags()
    assert clone.files("HEAD") == sample_repo.files("HEAD")
    for rel in sample_repo.files("HEAD"):
        assert (clone.root / rel).read_bytes() == (sample_repo.root / rel).read_bytes()
    assert clone.load_record("releases", "R1.0")["name"] == "R1.0"
    assert clone.load_record("devices", "SN-1")["release"] == "R1.0"
    assert remote.list_remotes(clone) == {"origin": str(origin)}


def test_second_push_is_idempotent(sample_repo, origin):
    remote.push(sample_repo, str(origin), holder="alice")
    res = remote.push(sample_repo, str(origin), holder="alice")
    assert res["branches"]["main"] == "up to date" and res["objects"] == 0


def test_pull_fast_forwards_working_tree(sample_repo, origin):
    remote.push(sample_repo, str(origin), holder="alice")
    clone = remote.clone(str(origin), origin.parent / "clone")
    (sample_repo.root / "NOTES.txt").write_text("new")
    new = sample_repo.commit("add notes", author="alice")
    remote.push(sample_repo, str(origin), holder="alice")
    res = remote.pull(clone, "origin")
    assert res["branches"]["main"].startswith("fast-forwarded")
    assert clone.head_id() == new.id and (clone.root / "NOTES.txt").read_text() == "new"


def test_diverged_push_and_pull_are_refused_not_merged(sample_repo, origin):
    remote.push(sample_repo, str(origin), holder="alice")
    clone = remote.clone(str(origin), origin.parent / "clone")
    (sample_repo.root / "a.txt").write_text("mine")
    sample_repo.commit("mine", author="alice")
    (clone.root / "b.txt").write_text("theirs")
    clone.commit("theirs", author="bob")
    remote.push(clone, "origin", holder="bob")
    with pytest.raises(VcmError, match="not an ancestor"):
        remote.push(sample_repo, str(origin), holder="alice")
    remote.add_remote(sample_repo, "origin", str(origin))
    with pytest.raises(VcmError, match="diverged|cannot fast-forward"):
        remote.pull(sample_repo, "origin")


def test_pull_refuses_to_clobber_dirty_working_tree(sample_repo, origin):
    remote.push(sample_repo, str(origin), holder="alice")
    clone = remote.clone(str(origin), origin.parent / "clone")
    (sample_repo.root / "x.txt").write_text("1")
    sample_repo.commit("x", author="alice")
    remote.push(sample_repo, str(origin), holder="alice")
    (clone.root / "x.txt").write_text("local edit")
    with pytest.raises(VcmError, match="uncommitted"):
        remote.pull(clone, "origin")


def test_push_blocked_by_remote_lock_held_by_someone_else(sample_repo, origin):
    remote.push(sample_repo, str(origin), holder="alice")
    target = "NOTES.txt"
    remote.remote_locks(origin).acquire(target, "bob", ttl=3600)
    (sample_repo.root / target).write_text("x")
    sample_repo.commit("notes", author="alice")
    with pytest.raises(VcmError, match="locked by bob"):
        remote.push(sample_repo, str(origin), holder="alice")
    assert remote.push(sample_repo, str(origin), holder="bob")["branches"]["main"].startswith("updated")


def test_tag_conflict_is_refused(sample_repo, origin):
    remote.push(sample_repo, str(origin), holder="alice")
    other = Repository.init(origin.parent / "other", branch="side")
    (other.root / "a.txt").write_text("1")
    other.commit("1")
    other.tag("v1.0")
    with pytest.raises(VcmError, match="tag v1.0"):
        remote.push(other, str(origin), holder="alice")
    assert "side" not in remote._refs(remote._side(origin), "heads")  # nothing was written


def test_corrupted_object_does_not_propagate(sample_repo, origin):
    blob = next(iter(sample_repo.store.iter_digests()))
    p = sample_repo.store.path(blob)
    p.chmod(0o644)
    p.write_bytes(b"corrupt")
    with pytest.raises(VcmError, match="corrupted"):
        remote.push(sample_repo, str(origin), holder="alice")


def test_release_signatures_merge_across_sides(sample_repo, origin, monkeypatch, tmp_path):
    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))
    create_release(sample_repo, "R1.0", "v1.0")
    remote.push(sample_repo, str(origin), holder="alice")
    signing.generate_key("alice")
    sign_release(sample_repo, "R1.0", "alice")
    remote.push(sample_repo, str(origin), holder="alice")
    clone = remote.clone(str(origin), origin.parent / "clone")
    assert len(clone.load_record("releases", "R1.0")["signatures"]) == 1


def test_immutable_release_cannot_be_overwritten(sample_repo, origin):
    create_release(sample_repo, "R1.0", "v1.0")
    remote.push(sample_repo, str(origin), holder="alice")
    m = sample_repo.load_record("releases", "R1.0")
    m["revision_message"] = "changed"
    sample_repo.save_record("releases", "R1.0", m)
    with pytest.raises(VcmError, match="immutable"):
        remote.push(sample_repo, str(origin), holder="alice")


def test_unsupported_transport_and_missing_remote(sample_repo):
    with pytest.raises(VcmError, match="unsupported"):
        remote.add_remote(sample_repo, "o", "s3://bucket/x")
    with pytest.raises(VcmError, match="not found"):
        remote.pull(sample_repo, "/nonexistent/remote")
    with pytest.raises(VcmError, match="no such remote"):
        remote.remove_remote(sample_repo, "nope")
