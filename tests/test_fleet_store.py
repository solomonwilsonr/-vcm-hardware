"""The shared fleet of record on Appwrite TablesDB.

The property every test here defends: a unit registered on one machine is visible to every other
machine, and a unit that could not be written to the fleet is visible nowhere. A per-clone registry
got both of those wrong, which is why the store exists.
"""
import json
import shutil
from pathlib import Path

import pytest

from fake_appwrite import AppwriteError, FakeTablesDB
from vcm_core import FileRecordStore, Repository, VcmError
from vcm_trace import define_rack, list_devices, place_device, register_device
from vcm_trace.fleet import list_racks
from vcm_trace.fleet_store import (
    DEVICES,
    RACKS,
    AppwriteConfig,
    AppwriteRecordStore,
    FleetUnreachable,
    fleet_is_shared,
    health,
    open_record_store,
    provision,
)
from vcm_workflow import create_release

NODE = Path(__file__).resolve().parent.parent / "examples" / "gpu-node"
CFG = AppwriteConfig(endpoint="http://x", project="p", api_key="k", database="vcm")


@pytest.fixture(params=[False, True], ids=["plain-dicts", "sdk-models"])
def fake(request):
    """The fleet tables under both SDK response shapes, as the graph tests do."""
    svc = FakeTablesDB(models=request.param)
    provision(CFG, svc)
    return svc


def _clone(tmp_path, name: str, fake, author: str = "t", when: str | None = None) -> Repository:
    """A checkout of the same design, with its own disk and the shared fleet attached.

    Commits are content-addressed and deterministic, so two clones built identically share a
    revision id. ``author``/``when`` let a test give a clone a genuinely separate history, which is
    the normal case once engineers commit independently.
    """
    r = Repository.init(tmp_path / name)
    shutil.copytree(NODE / "v1.0", r.root, dirs_exist_ok=True)
    r.commit("v1.0", author=author, timestamp=when) if when else r.commit("v1.0", author=author)
    r.tag("v1.0")
    create_release(r, "R1", "v1.0")
    r.records = AppwriteRecordStore(CFG, service=fake, base=r.dir)
    return r


def test_provision_creates_fleet_schema_and_is_idempotent(fake):
    assert set(fake.tables) == {DEVICES, RACKS}
    assert "release" in fake.columns[DEVICES] and "doc" in fake.columns[DEVICES]
    again = provision(CFG, fake)
    assert again["created"] == [] and again["existed"]


def test_registering_a_unit_writes_to_tablesdb(fake, tmp_path):
    repo = _clone(tmp_path, "a", fake)
    register_device(repo, "NODE-0001", "R1", batch="RACK-12")
    assert fake.row_count(DEVICES) == 1
    row = next(iter(fake.tables[DEVICES].values()))
    assert row["name"] == "NODE-0001" and row["release"] == "R1"
    assert json.loads(row["doc"])["serial"] == "NODE-0001"


def test_a_second_clone_sees_the_whole_fleet(fake, tmp_path):
    """The reason this store exists: two engineers, one fleet.

    With the old per-clone JSON registry, ``b`` saw an empty fleet and a recall query run there
    would have reported no affected units at all.
    """
    a = _clone(tmp_path, "a", fake)
    b = _clone(tmp_path, "b", fake)
    register_device(a, "NODE-0001", "R1")
    register_device(b, "NODE-0002", "R1")

    assert [d["serial"] for d in list_devices(a)] == ["NODE-0001", "NODE-0002"]
    assert [d["serial"] for d in list_devices(b)] == ["NODE-0001", "NODE-0002"]
    # and the local mirrors hold only what each machine wrote
    assert FileRecordStore(a.dir).has("devices", "NODE-0001")
    assert not FileRecordStore(a.dir).has("devices", "NODE-0002")


def test_duplicate_serial_is_rejected_across_clones(fake, tmp_path):
    a = _clone(tmp_path, "a", fake)
    b = _clone(tmp_path, "b", fake)
    register_device(a, "NODE-0001", "R1")
    with pytest.raises(VcmError, match="already registered"):
        register_device(b, "NODE-0001", "R1")


def test_racks_and_placement_are_shared(fake, tmp_path):
    a = _clone(tmp_path, "a", fake)
    b = _clone(tmp_path, "b", fake)
    define_rack(a, "RACK-A", power_budget_w=9000)
    register_device(a, "NODE-0001", "R1")
    place_device(a, "NODE-0001", "RACK-A", position=3)

    assert [r["name"] for r in list_racks(b)] == ["RACK-A"]
    assert b.load_record("devices", "NODE-0001")["location"] == {"rack": "RACK-A", "position": 3}
    row = next(iter(fake.tables[DEVICES].values()))
    assert row["rack"] == "RACK-A"      # indexed facet, so "which units are in this rack" is a query


class _Down:
    """A service whose every call fails, standing in for a lost connection."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        def boom(*a, **kw):
            raise AppwriteError("connection refused")
        return boom


def test_writes_fail_closed_when_the_fleet_is_unreachable(fake, tmp_path):
    """A unit the fleet never heard of must not exist locally either."""
    repo = _clone(tmp_path, "a", fake)
    repo.records = AppwriteRecordStore(CFG, service=_Down(fake), base=repo.dir)
    with pytest.raises(FleetUnreachable, match="fleet of record"):
        register_device(repo, "NODE-0001", "R1")
    assert not FileRecordStore(repo.dir).has("devices", "NODE-0001")
    assert fake.row_count(DEVICES) == 0


def test_reads_degrade_to_the_local_cache(fake, tmp_path):
    repo = _clone(tmp_path, "a", fake)
    register_device(repo, "NODE-0001", "R1")

    offline = AppwriteRecordStore(CFG, service=_Down(fake), base=repo.dir)
    repo.records = offline
    assert [d["serial"] for d in list_devices(repo)] == ["NODE-0001"]   # from the mirror
    assert offline.degraded and "local cache" in offline.degraded


def test_repository_scoped_kinds_stay_local(fake, tmp_path):
    """Releases travel with the checkout, so they must not reach the fleet tables."""
    repo = _clone(tmp_path, "a", fake)
    assert repo.load_record("releases", "R1")["name"] == "R1"
    assert set(fake.tables) == {DEVICES, RACKS}
    assert fake.row_count(DEVICES) == 0


def test_by_release_filters_server_side(fake, tmp_path):
    repo = _clone(tmp_path, "a", fake)
    create_release(repo, "R2", "v1.0")
    register_device(repo, "NODE-0001", "R1")
    register_device(repo, "NODE-0002", "R2")
    store = repo.records
    assert [d["serial"] for d in store.by_release("R2")] == ["NODE-0002"]


def test_store_selection_falls_back_to_local_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("VCM_FLEET_LOCAL", raising=False)   # test the credentials path, not the flag
    for var in AppwriteConfig.ENV.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("vcm_graph.backends.appwrite_backend.load_dotenv", lambda *a, **k: {})
    assert not fleet_is_shared(AppwriteConfig(dotenv=None))
    assert open_record_store(tmp_path, AppwriteConfig(dotenv=None)).name == "local"


def test_local_override_keeps_the_fleet_in_the_checkout(monkeypatch):
    monkeypatch.setenv("VCM_FLEET_LOCAL", "1")
    assert not fleet_is_shared(CFG)


def test_health_reports_size_and_sharing(fake, tmp_path):
    repo = _clone(tmp_path, "a", fake)
    register_device(repo, "NODE-0001", "R1")
    rep = health(CFG, fake)
    assert rep["ok"] and rep["configured"] and rep["units"] == 1 and rep["missing_tables"] == []


def test_health_is_explicit_when_unconfigured(monkeypatch):
    # a developer's real .env may already be in os.environ from an earlier test in this process
    for var in AppwriteConfig.ENV.values():
        monkeypatch.delenv(var, raising=False)
    rep = health(AppwriteConfig(dotenv=None, database="vcm"))
    assert rep["ok"] is False and rep["configured"] is False and rep["missing"]


def test_a_unit_from_an_unknown_revision_is_reported_not_fatal(fake, tmp_path):
    """Two clones, independent histories: ``b`` meets a unit whose revision it has never fetched.

    The recall must still answer for the units it can describe, and name the one it cannot, because
    an incident closed on a silently partial fleet is the failure this whole store exists to stop.
    """
    from vcm_trace import recall

    a = _clone(tmp_path, "a", fake, author="alice", when="2026-01-10T10:00:00Z")
    b = _clone(tmp_path, "b", fake, author="bob", when="2026-02-10T10:00:00Z")
    register_device(a, "NODE-AAA", "R1")
    register_device(b, "NODE-BBB", "R1")
    assert a.load_record("devices", "NODE-AAA")["revision"] != \
        b.load_record("devices", "NODE-BBB")["revision"]

    res = recall(b, model="A100")
    assert [u["serial"] for u in res["units"]] == ["NODE-BBB"]
    assert [u["serial"] for u in res["unresolved"]] == ["NODE-AAA"]
