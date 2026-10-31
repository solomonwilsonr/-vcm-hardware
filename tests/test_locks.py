import pytest

from vcm_core import LockStore, VcmError
from vcm_core.locks import normalize


def test_lock_conflict_renew_release(tmp_path):
    s = LockStore(tmp_path / "locks")
    lock = s.acquire("board/main.kicad_pcb", "alice", ttl=100, now=1000.0)
    assert lock["holder"] == "alice"
    with pytest.raises(VcmError, match="locked by alice"):
        s.acquire("board/main.kicad_pcb", "bob", now=1050.0)
    renewed = s.acquire("board/main.kicad_pcb", "alice", ttl=500, now=1050.0)
    assert renewed["acquired_at"] == lock["acquired_at"] and renewed["expires_ts"] == 1550.0
    with pytest.raises(VcmError, match="not bob"):
        s.release("board/main.kicad_pcb", "bob", now=1060.0)
    s.release("board/main.kicad_pcb", "bob", force=True, now=1060.0)
    assert s.list(now=1060.0) == []


def test_expired_lock_is_reclaimable(tmp_path):
    s = LockStore(tmp_path / "locks")
    s.acquire("a.txt", "alice", ttl=10, now=1000.0)
    assert s.get("a.txt", now=1011.0) is None
    assert s.acquire("a.txt", "bob", ttl=10, now=1011.0)["holder"] == "bob"


def test_lock_needs_identity_and_safe_path(tmp_path):
    s = LockStore(tmp_path / "locks")
    for bad in ("", "unknown"):
        with pytest.raises(VcmError):
            s.acquire("a.txt", bad)
    for bad in ("../x", "/etc/passwd", ""):
        with pytest.raises(VcmError):
            normalize(bad)
    assert normalize("./a//b".replace("//", "/")) == "a/b"


def test_commit_blocked_by_someone_elses_lock(repo):
    (repo.root / "bom.csv").write_text("Reference,Value\nR1,1k\n")
    repo.commit("init", author="alice")
    repo.locks.acquire("bom.csv", "alice", ttl=3600)
    (repo.root / "bom.csv").write_text("Reference,Value\nR1,2k\n")
    with pytest.raises(VcmError, match="locked by alice"):
        repo.commit("edit", author="bob")
    assert repo.commit("edit", author="alice").message == "edit"


def test_commit_unaffected_by_lock_on_other_file(repo):
    (repo.root / "a.txt").write_text("1")
    (repo.root / "b.txt").write_text("1")
    repo.commit("init", author="alice")
    repo.locks.acquire("a.txt", "alice", ttl=3600)
    (repo.root / "b.txt").write_text("2")
    assert repo.commit("edit b", author="bob")


def test_protected_branch_refuses_direct_commit(repo):
    (repo.root / "a.txt").write_text("1")
    repo.commit("init")
    cfg = repo.config()
    cfg["protected_branches"] = ["main"]
    repo.save_config(cfg)
    (repo.root / "a.txt").write_text("2")
    with pytest.raises(VcmError, match="protected"):
        repo.commit("direct")
