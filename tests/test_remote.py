import json

import pytest

from vcm_cli.main import main
from vcm_core import Repository, VcmError
from vcm_core import remote as rm


@pytest.fixture
def team(tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_AUTHOR", "alice")
    shared = tmp_path / "shared"
    alice = Repository.init(tmp_path / "alice")
    (alice.root / "node.json").write_text('{"v": 1}\n')
    (alice.root / "notes.md").write_text("hello\n")
    alice.commit("first", author="alice")
    alice.tag("v1")
    rm.add_remote(alice, "origin", str(shared))
    rm.push(alice, "origin")
    bob = Repository.init(tmp_path / "bob")
    rm.add_remote(bob, "origin", "file://" + str(shared))
    rm.pull(bob, "origin")
    return alice, bob, shared


def _edit_commit(repo, path, text, msg, author):
    (repo.root / path).write_text(text)
    return repo.commit(msg, author=author)


def test_push_and_pull_fast_forward(team):
    alice, bob, shared = team
    assert (bob.root / "node.json").read_text() == '{"v": 1}\n'
    assert bob.tags() == {"v1": alice.resolve("v1")}
    assert (shared / "refs" / "heads" / "main").read_text().strip() == alice.head_id()

    _edit_commit(bob, "node.json", '{"v": 2}\n', "bump", "bob")
    (bob.root / "notes.md").unlink()
    bob.commit("drop notes", author="bob")
    res = rm.push(bob, "origin")
    assert res["from"] == alice.head_id() and res["objects"] > 0
    pulled = rm.pull(alice, "origin")
    assert pulled["fast_forward"] and alice.head_id() == bob.head_id()
    assert (alice.root / "node.json").read_text() == '{"v": 2}\n'
    assert not (alice.root / "notes.md").exists()   # deletions travel too
    assert rm.push(alice, "origin")["up_to_date"]


def test_divergence_is_refused_not_merged(team):
    alice, bob, _ = team
    _edit_commit(bob, "node.json", '{"v": "bob"}\n', "bob change", "bob")
    rm.push(bob, "origin")
    _edit_commit(alice, "node.json", '{"v": "alice"}\n', "alice change", "alice")
    with pytest.raises(VcmError, match="push rejected.*does not merge"):
        rm.push(alice, "origin")
    with pytest.raises(VcmError, match="diverged"):
        rm.pull(alice, "origin")


def test_pull_refuses_dirty_tree(team):
    alice, bob, _ = team
    _edit_commit(bob, "node.json", '{"v": 3}\n', "bob", "bob")
    rm.push(bob, "origin")
    (alice.root / "node.json").write_text("local edit\n")
    with pytest.raises(VcmError, match="uncommitted"):
        rm.pull(alice, "origin")


def test_locks_block_others_and_expire(team, monkeypatch, capsys):
    alice, bob, shared = team
    remote = rm.Remote(shared)
    rm.acquire_lock(remote, "node.json", 4, "re-pinning PSUs", holder="alice")
    with pytest.raises(VcmError, match="locked by alice"):
        rm.acquire_lock(remote, "node.json", holder="bob")

    monkeypatch.setenv("VCM_AUTHOR", "bob")
    (bob.root / "node.json").write_text('{"v": "bob"}\n')
    assert main(["-C", str(bob.root), "commit", "-m", "try"]) == 1
    assert "locked on origin by alice" in capsys.readouterr().err
    assert main(["-C", str(bob.root), "commit", "-m", "local", "--ignore-locks"]) == 0
    with pytest.raises(VcmError, match="node.json is locked by alice"):
        rm.push(bob, "origin")
    # unrelated files are not blocked by the lock
    with pytest.raises(VcmError, match="not bob"):
        rm.release_lock(remote, "node.json", holder="bob")
    rm.release_lock(remote, "node.json", holder="alice")
    assert rm.push(bob, "origin")["to"] == bob.head_id()

    rm.acquire_lock(remote, "notes.md", ttl_hours=0, holder="alice")
    assert rm.list_locks(remote)[0]["expired"]
    assert rm.blocking_locks(remote, ["notes.md"], "bob") == []


def test_records_sync_without_overwrites(team):
    alice, bob, _ = team
    alice.save_record("releases", "R1", {"name": "R1", "revision": alice.head_id()})
    assert rm.push(alice, "origin")["records"] == ["releases/R1"]
    assert rm.fetch(bob, "origin")["records"] == ["releases/R1"]
    bob.save_record("releases", "R1", {"name": "R1", "revision": "something else"})
    res = rm.fetch(bob, "origin")
    assert res["record_conflicts"] == ["releases/R1"]
    assert bob.load_record("releases", "R1")["revision"] == "something else"


def test_tag_conflict_reported(team):
    alice, bob, _ = team
    _edit_commit(bob, "node.json", '{"v": 9}\n', "x", "bob")
    bob.tag("v2")
    rm.push(bob, "origin")
    _edit_commit(alice, "notes.md", "other\n", "y", "alice")
    alice.tag("v2")
    res = rm.fetch(alice, "origin")
    assert res["tag_conflicts"] == ["v2"]
    assert alice.tags()["v2"] == alice.head_id()


def test_corrupted_remote_object_is_refused(team, tmp_path):
    alice, bob, shared = team
    _edit_commit(alice, "node.json", '{"v": 5}\n', "five", "alice")
    rm.push(alice, "origin")
    target = next(p for p in (shared / "objects").rglob("*")
                  if p.is_file() and not bob.store.has(p.parent.name + p.name))
    target.chmod(0o644)
    target.write_bytes(b"tampered")
    with pytest.raises(VcmError, match="corrupted"):
        rm.fetch(bob, "origin")


def test_cli_remote_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VCM_AUTHOR", "carol")
    a = tmp_path / "a"
    assert main(["init", str(a)]) == 0
    (a / "x.json").write_text(json.dumps({"k": 1}))
    assert main(["-C", str(a), "commit", "-m", "x"]) == 0
    assert main(["-C", str(a), "remote", "add", "origin", str(tmp_path / "srv")]) == 0
    assert main(["-C", str(a), "push"]) == 0
    assert main(["-C", str(a), "lock", "acquire", "x.json", "--reason", "editing"]) == 0
    assert main(["-C", str(a), "lock", "list"]) == 0
    assert "editing" in capsys.readouterr().out
    assert main(["-C", str(a), "lock", "release", "x.json"]) == 0
    b = tmp_path / "b"
    assert main(["init", str(b)]) == 0
    assert main(["-C", str(b), "remote", "add", "origin", str(tmp_path / "srv")]) == 0
    assert main(["-C", str(b), "pull"]) == 0
    assert json.loads((b / "x.json").read_text()) == {"k": 1}
    actions = [e["action"] for e in Repository(a).audit.events()]
    assert {"push", "lock.acquire", "lock.release"} <= set(actions)


def test_evidence_merges_across_clones(team):
    alice, bob, _ = team
    rid = alice.head_id()
    alice.attach_evidence(rid, "validation", {"status": "pass", "by": "alice"})
    rm.push(alice, "origin")
    bob.attach_evidence(rid, "validation", {"status": "fail", "by": "bob"})
    assert rm.fetch(bob, "origin")["evidence"] == 1
    hist = bob.evidence_history(rid, "validation")
    assert [e["version"] for e in hist] == [1, 2] and len({e["hash"] for e in hist}) == 2
    assert bob.latest_evidence(rid, "validation")["status"] in ("pass", "fail")
