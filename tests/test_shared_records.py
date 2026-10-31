"""Releases, devices, PRs, locks and the change feed on Appwrite TablesDB (offline, via the fake).

The contract: with ``VCM_RECORDS_BACKEND=appwrite`` every feature behaves exactly as it does on
local files, but the state lives in the shared store, so two checkouts see the same locks and PRs.
"""
import threading

import pytest

from fake_appwrite import FakeTablesDB
from vcm_core import Repository, VcmError, records
from vcm_core.records import LocalRecords, copy_records
from vcm_graph.backends.appwrite_backend import AppwriteConfig, provision
from vcm_graph.backends.appwrite_records import AppwriteRecords, realtime_config
from vcm_graph.schema import EVENTS, RECORDS
from vcm_trace import register_device
from vcm_workflow import approve, create_release, get_pr, merge_pr, open_pr

CFG = AppwriteConfig(endpoint="http://x", project="p", api_key="k", database="vcm", dotenv=None)


@pytest.fixture(params=[False, True], ids=["plain-dicts", "sdk-models"])
def fake(request):
    svc = FakeTablesDB(models=request.param)
    provision(CFG, svc, public_events=False)
    return svc


@pytest.fixture
def shared(fake, monkeypatch):
    """A factory: every repository in the test talks to the same fake Appwrite."""
    monkeypatch.setenv("VCM_RECORDS_BACKEND", "appwrite")
    records.register_backend("appwrite", lambda repo: AppwriteRecords(CFG, fake, scope="team-a"))
    yield fake
    monkeypatch.delenv("VCM_RECORDS_BACKEND")


def test_records_roundtrip_exclusive_create_and_delete(fake):
    r = AppwriteRecords(CFG, fake, scope="s")
    assert r.get("releases", "R1") is None
    assert r.create("releases", "R1", {"name": "R1"}) is True
    assert r.create("releases", "R1", {"name": "other"}) is False  # the database refuses a duplicate id
    assert r.get("releases", "R1") == {"name": "R1"}
    r.put("releases", "R1", {"name": "R1", "x": 1})
    assert r.list("releases") == [{"name": "R1", "x": 1}] and r.names("releases") == ["R1"]
    assert r.delete("releases", "R1") is True and r.delete("releases", "R1") is False
    assert [e["action"] for e in r.events()] == ["created", "updated", "deleted"]


def test_scopes_isolate_projects_sharing_one_database(fake):
    a, b = AppwriteRecords(CFG, fake, scope="a"), AppwriteRecords(CFG, fake, scope="b")
    a.put("prs", "1", {"id": 1})
    assert b.get("prs", "1") is None and b.list("prs") == [] and b.events() == []
    assert a.list("prs") == [{"id": 1}]


def test_large_record_survives(fake):
    big = {"diff": "x" * 300_000}
    AppwriteRecords(CFG, fake).put("prs", "1", big)
    assert AppwriteRecords(CFG, fake).get("prs", "1") == big


def test_listing_pages_past_one_page(fake):
    r = AppwriteRecords(CFG, fake, scope="s", feed=False)
    for i in range(250):
        r.put("devices", f"SN-{i:03d}", {"serial": f"SN-{i:03d}"})
    assert len(r.list("devices")) == 250 == len(r.names("devices"))


def test_events_are_payload_free_ordered_and_pageable(fake):
    r = AppwriteRecords(CFG, fake, scope="s")
    r.put("prs", "1", {"author": "alice", "secret": "do-not-leak"})
    r.put("locks", "k", {"holder": "bob"})
    evs = r.events()
    assert [(e["kind"], e["actor"]) for e in evs] == [("prs", "alice"), ("locks", "bob")]
    assert "do-not-leak" not in str(fake.tables[EVENTS])
    assert r.events(after=evs[0]["at"]) == [evs[1]] and r.events(after=evs[1]["at"]) == []


def test_event_failure_never_fails_the_write(fake):
    r = AppwriteRecords(CFG, fake, scope="s")
    del fake.tables[EVENTS]
    r.put("prs", "1", {"id": 1})  # the feed is gone; the record must still be saved
    assert r.get("prs", "1") == {"id": 1} and r.last_event_error


def test_everything_works_on_the_shared_store(sample_repo, shared):
    r = sample_repo
    create_release(r, "R1.0", "v1.0")
    register_device(r, "SN-1", "R1.0", "B")
    assert shared.row_count(RECORDS) == 2
    assert not (r.dir / "releases").exists()  # nothing was written to local record files

    r.branch("feature", "v1.1")
    r.checkout("v1.0", force=True)
    r._set_ref("heads", "main", r.resolve("v1.0"))
    (r.dir / "HEAD").write_text("ref: refs/heads/main\n")
    pr = open_pr(r, "main", "feature", "swap", author="bob")
    approve(r, pr["id"], "alice")
    assert get_pr(r, pr["id"])["status"]["ready_for_merge"]
    assert merge_pr(r, pr["id"], "carol")["state"] == "merged"
    assert r.branches()["main"] == r.resolve("v1.1")

    r.locks.acquire("a.txt", "alice", ttl=3600)
    with pytest.raises(VcmError, match="locked by alice"):
        r.locks.acquire("a.txt", "bob", ttl=3600)
    (r.root / "a.txt").write_text("x")
    with pytest.raises(VcmError, match="locked by someone else"):
        r.commit("edit", author="bob")


def test_two_checkouts_share_locks_and_prs(sample_repo, shared, tmp_path):
    other = _clone(sample_repo, tmp_path)
    sample_repo.locks.acquire("fw.bin", "alice", ttl=3600)
    with pytest.raises(VcmError, match="locked by alice"):
        other.locks.acquire("fw.bin", "bob", ttl=3600)  # a different checkout, same store
    assert [lk["holder"] for lk in other.locks.list()] == ["alice"]
    create_release(sample_repo, "R1", "v1.0")
    assert other.load_record("releases", "R1")["name"] == "R1"


def _clone(repo, tmp_path):
    import shutil
    dst = tmp_path / "second"
    shutil.copytree(repo.root, dst)
    return Repository(dst)


def test_concurrent_lock_acquire_has_one_winner(fake):
    store = AppwriteRecords(CFG, fake, scope="s")
    from vcm_core import LockStore
    results, barrier = [], threading.Barrier(8)

    def grab(who):
        barrier.wait()
        try:
            LockStore(store).acquire("hot.kicad_pcb", who, ttl=3600)
            results.append(who)
        except VcmError:
            pass

    threads = [threading.Thread(target=grab, args=(f"u{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(results) == 1


def test_migrate_local_records_to_appwrite_without_overwriting(sample_repo, fake):
    create_release(sample_repo, "R1.0", "v1.0")
    register_device(sample_repo, "SN-1", "R1.0")
    dst = AppwriteRecords(CFG, fake, scope="s")
    rep = copy_records(LocalRecords(sample_repo.dir), dst)
    assert sorted(rep["copied"]) == ["devices/SN-1", "releases/R1.0"] and not rep["conflicts"]
    again = copy_records(LocalRecords(sample_repo.dir), dst)
    assert again["copied"] == [] and len(again["skipped"]) == 2
    dst.put("releases", "R1.0", {"name": "R1.0", "changed": True})
    assert copy_records(LocalRecords(sample_repo.dir), dst)["conflicts"] == ["releases/R1.0"]


def test_local_backend_has_the_same_event_feed(sample_repo):
    create_release(sample_repo, "R1.0", "v1.0")
    sample_repo.locks.acquire("a.txt", "alice", ttl=60)
    evs = sample_repo.records.events()
    assert [(e["kind"], e["action"]) for e in evs] == [("releases", "created"), ("locks", "created")]
    assert sample_repo.records.events(after=evs[-1]["at"]) == []


def test_unknown_backend_and_realtime_is_opt_in(sample_repo, monkeypatch):
    monkeypatch.setenv("VCM_RECORDS_BACKEND", "nope")
    with pytest.raises(VcmError, match="not available"):
        _ = sample_repo.records
    monkeypatch.delenv("VCM_APPWRITE_REALTIME", raising=False)
    assert realtime_config(CFG) == {"enabled": False}
    monkeypatch.setenv("VCM_APPWRITE_REALTIME", "1")
    cfg = realtime_config(CFG, scope="team-a")
    assert cfg["enabled"] and cfg["scope"] == "team-a" and "api_key" not in str(cfg)
    assert any(c.endswith(f".{EVENTS}.rows") for c in cfg["channels"])


def test_events_table_is_private_unless_realtime_is_requested():
    private, public = FakeTablesDB(), FakeTablesDB()
    provision(CFG, private, public_events=False)
    provision(CFG, public, public_events=True)
    assert private.table_permissions[EVENTS] is None and private.table_permissions[RECORDS] is None
    assert public.table_permissions[EVENTS] == ['read("any")']
    assert public.table_permissions[RECORDS] is None  # record content is never public
