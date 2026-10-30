import pytest

from vcm_core import NothingToCommit, ObjectStore, Repository, VcmError, categorize


def test_object_store_dedup_and_verify(tmp_path):
    s = ObjectStore(tmp_path / "o")
    h1 = s.put_bytes(b"hello")
    h2 = s.put_bytes(b"hello")
    assert h1 == h2 and len(list(s.iter_digests())) == 1
    assert s.read(h1) == b"hello" and s.verify(h1)


def test_put_file_streams_large(tmp_path):
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (3 * (1 << 20) + 5))
    s = ObjectStore(tmp_path / "o")
    h, size = s.put_file(big)
    assert size == big.stat().st_size and s.verify(h)
    assert s.put_file(big)[0] == h


def test_corruption_detected(tmp_path):
    s = ObjectStore(tmp_path / "o")
    h = s.put_bytes(b"data")
    p = s.path(h)
    p.chmod(0o644)
    p.write_bytes(b"evil")
    assert not s.verify(h)


def test_categorize():
    assert categorize("a/b.kicad_pcb") == "pcb"
    assert categorize("bom/bom_v1.csv") == "electronics"
    assert categorize("firmware/main.c") == "firmware"
    assert categorize("notes.md") == "docs"
    assert categorize("fpga/x.bin") == "fpga"


def test_commit_log_diff_checkout(repo):
    (repo.root / "a.txt").write_text("one")
    r1 = repo.commit("first", timestamp="2026-01-01T00:00:00Z")
    (repo.root / "a.txt").write_text("two")
    (repo.root / "firmware").mkdir()
    (repo.root / "firmware" / "m.c").write_text("int main(){}")
    r2 = repo.commit("second", timestamp="2026-01-02T00:00:00Z")
    assert [r.id for r in repo.log()] == [r2.id, r1.id]
    assert repo.resolve("HEAD~1") == r1.id and repo.resolve(r2.id[:8]) == r2.id
    d = repo.diff_artifacts(r1.id, r2.id)
    assert [x["path"] for x in d["added"]] == ["firmware/m.c"]
    assert [x["path"] for x in d["modified"]] == ["a.txt"]
    assert r2.artifacts["firmware/m.c"].category == "firmware"
    repo.branch("old", r1.id)
    repo.checkout("old")
    assert (repo.root / "a.txt").read_text() == "one"
    assert not (repo.root / "firmware" / "m.c").exists()
    assert repo.current_branch == "old"


def test_nothing_to_commit_and_dirty_checkout(repo):
    (repo.root / "a.txt").write_text("one")
    repo.commit("first")
    with pytest.raises(NothingToCommit):
        repo.commit("again")
    repo.branch("b2")
    (repo.root / "a.txt").write_text("dirty")
    with pytest.raises(VcmError):
        repo.checkout("b2")
    repo.checkout("b2", force=True)


def test_revision_id_is_deterministic(tmp_path):
    ids = []
    for n in ("x", "y"):
        r = Repository.init(tmp_path / n)
        (r.root / "f").write_text("same")
        ids.append(r.commit("m", author="a", timestamp="2026-01-01T00:00:00Z").id)
    assert ids[0] == ids[1]


def test_tags_branches_and_evidence_versions(repo):
    (repo.root / "f").write_text("1")
    r = repo.commit("m")
    repo.tag("v1")
    assert repo.resolve("v1") == r.id and "main" in repo.branches()
    repo.attach_evidence("v1", "validation", {"status": "fail"})
    repo.attach_evidence("v1", "validation", {"status": "pass"})
    assert len(repo.evidence_history("v1", "validation")) == 2
    assert repo.latest_evidence("v1", "validation")["status"] == "pass"


def test_discover(repo):
    sub = repo.root / "x" / "y"
    sub.mkdir(parents=True)
    assert Repository.discover(sub).root == repo.root


def test_record_names_cannot_escape(repo):
    (repo.dir / "secret.json").write_text("{}")
    for bad in ("../secret", "a/b", ".hidden", ""):
        with pytest.raises(VcmError):
            repo.load_record("releases", bad)
    repo.save_record("releases", "ok", {"name": "ok"})
    assert repo.load_record("releases", "ok") == {"name": "ok"}
