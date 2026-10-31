"""The Appwrite TablesDB backend, exercised offline against a faithful fake service.

The contract these tests pin down: an Appwrite-backed graph must answer every query exactly as the
local SQLite graph does, so impact analysis is identical whichever store a team runs on.
"""
import pytest

from fake_appwrite import AppwriteError, FakeTablesDB
from vcm_graph import build_graph
from vcm_graph.backends.appwrite_backend import (
    AppwriteBackend,
    AppwriteConfig,
    AppwriteConfigError,
    health,
    load_dotenv,
    provision,
)
from vcm_graph.schema import EDGES, NODES


@pytest.fixture(params=[False, True], ids=["plain-dicts", "sdk-models"])
def fake(request):
    """The service under both response shapes: plain dicts and SDK ``Row``/``Table`` models.

    The live SDK returns models whose columns sit under ``.data``; a fake that only ever returned
    dicts let a crash reach production once, so every test runs against both.
    """
    svc = FakeTablesDB(models=request.param)
    provision(AppwriteConfig(endpoint="http://x", project="p", api_key="k", database="vcm"), svc)
    return svc


def _backend(fake, **kw):
    return AppwriteBackend(AppwriteConfig(endpoint="http://x", project="p", api_key="k",
                                          database="vcm"), service=fake, **kw)


def test_provision_creates_schema_and_is_idempotent(fake):
    assert set(fake.tables) == {NODES, EDGES}
    assert set(fake.columns[NODES]) == {"rev", "node_id", "type", "name", "props"}
    assert set(fake.columns[EDGES]) == {"rev", "src", "dst", "type", "props"}
    assert set(fake.indexes[EDGES]) == {"edge_rev_src", "edge_rev_dst"}
    assert fake.indexes[EDGES]["edge_rev_src"][0] == ("rev", "src", "type")

    cfg = AppwriteConfig(endpoint="http://x", project="p", api_key="k", database="vcm")
    again = provision(cfg, fake)
    assert again["created"] == []
    assert len(again["existed"]) == 17  # 1 database + 2 tables + 10 columns + 4 indexes


def test_graph_matches_sqlite_exactly(sample_repo, fake):
    local = build_graph(sample_repo, "v1.0")
    remote = build_graph(sample_repo, "v1.0", backend=_backend(fake))

    assert remote.stats()["nodes"] == local.stats()["nodes"]
    assert remote.stats()["edges"] == local.stats()["edges"]
    assert remote.stats()["backend"] == "appwrite"
    assert remote.node("net:GND")["ground"] is True
    assert remote.node("component:U1")["value"] == "AMS1117-3.3"
    assert remote.out("component:U2", "configured_by") == local.out("component:U2", "configured_by")
    assert remote.into("net:+3V3") == local.into("net:+3V3")
    assert remote.nodes("component") == local.nodes("component")
    assert remote.affected_by("U1") == local.affected_by("U1")
    assert remote.affected_by("D1") == local.affected_by("D1")


def test_live_queries_match_prefetched_reads(sample_repo, fake):
    build_graph(sample_repo, "v1.0", backend=_backend(fake))
    cached = build_graph(sample_repo, "v1.0", backend=_backend(fake, prefetch=True))
    live = build_graph(sample_repo, "v1.0", backend=_backend(fake, prefetch=False))
    assert live.affected_by("U1") == cached.affected_by("U1")
    assert live.stats()["nodes"] == cached.stats()["nodes"]
    assert live.node("component:U1") == cached.node("component:U1")
    assert live.node("component:NOPE") is None


def test_rebuild_is_idempotent_not_duplicating_rows(sample_repo, fake):
    build_graph(sample_repo, "v1.0", backend=_backend(fake))
    nodes, edges = fake.row_count(NODES), fake.row_count(EDGES)
    build_graph(sample_repo, "v1.0", backend=_backend(fake))
    assert (fake.row_count(NODES), fake.row_count(EDGES)) == (nodes, edges)


def test_revisions_stay_isolated_in_one_store(sample_repo, fake):
    g1 = build_graph(sample_repo, "v1.0", backend=_backend(fake))
    g2 = build_graph(sample_repo, "v1.1", backend=_backend(fake))
    assert g1.node("component:U1")["value"] == "AMS1117-3.3"
    assert g2.node("component:U1")["value"] == "MCP1700-3302E/TT"
    assert fake.row_count(NODES) > len(g1.nodes())  # both revisions share the table


def test_writes_are_batched_not_one_request_per_row(sample_repo, fake):
    fake.reset_requests()
    build_graph(sample_repo, "v1.0", backend=_backend(fake))
    upserts = fake.ops("upsert")
    assert sum(n for _, _, n in upserts) > 100          # a few hundred rows went out
    assert len(upserts) <= 8                            # in a handful of bulk requests
    assert all(n <= 100 for _, _, n in upserts)         # respecting the batch size


def test_clear_only_removes_its_own_revision(sample_repo, fake):
    build_graph(sample_repo, "v1.0", backend=_backend(fake))
    g2 = build_graph(sample_repo, "v1.1", backend=_backend(fake))
    before = fake.row_count(NODES)
    g2.clear()
    g2.flush()
    assert 0 < fake.row_count(NODES) < before
    assert build_graph(sample_repo, "v1.0", backend=_backend(fake)).node("component:U1") is not None


def test_config_is_incomplete_without_credentials(monkeypatch):
    for var in AppwriteConfig.ENV.values():
        monkeypatch.delenv(var, raising=False)
    cfg = AppwriteConfig(dotenv=None)
    assert not cfg.complete
    assert "VCM_APPWRITE_ENDPOINT" in cfg.missing()
    with pytest.raises(AppwriteConfigError, match="not configured"):
        cfg.require()
    assert health(cfg) == {"ok": False, "configured": False, "missing": cfg.missing()}


def test_config_reads_environment(monkeypatch):
    for key, var in AppwriteConfig.ENV.items():
        monkeypatch.setenv(var, f"env-{key}")
    cfg = AppwriteConfig(dotenv=None)
    assert cfg.complete and cfg.endpoint == "env-endpoint" and cfg.database == "env-database"


def test_health_reports_provisioned_store(fake):
    cfg = AppwriteConfig(endpoint="http://x", project="p", api_key="k", database="vcm")
    assert health(cfg, fake) == {"ok": True, "configured": True, "endpoint": "http://x",
                                 "database": "vcm", "tables": [EDGES, NODES], "missing_tables": []}


def test_health_reports_missing_tables():
    cfg = AppwriteConfig(endpoint="http://x", project="p", api_key="k", database="vcm")
    svc = FakeTablesDB(models=True)
    svc.create("vcm", "VCM")          # the database exists; only one of its tables does
    svc.create_table("vcm", NODES, NODES)
    report = health(cfg, svc)
    assert report["ok"] is False and report["missing_tables"] == [EDGES]


def test_unknown_table_surfaces_the_error(fake):
    with pytest.raises(AppwriteError):
        fake.upsert_rows("vcm", "nope", [{"$id": "a"}])


def test_dotenv_is_loaded_without_overriding_the_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# credentials\n"
                   "export VCM_APPWRITE_ENDPOINT=\"https://fra.cloud.appwrite.io/v1\"\n"
                   "VCM_APPWRITE_PROJECT='proj123'\n"
                   "VCM_APPWRITE_API_KEY=secret\n"
                   "\n"
                   "not-a-pair\n")
    for var in AppwriteConfig.ENV.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("VCM_APPWRITE_PROJECT", "already-set")

    loaded = load_dotenv(env)
    assert loaded["VCM_APPWRITE_ENDPOINT"] == "https://fra.cloud.appwrite.io/v1"
    assert "not-a-pair" not in loaded

    cfg = AppwriteConfig(dotenv=env)
    assert cfg.endpoint == "https://fra.cloud.appwrite.io/v1"
    assert cfg.api_key == "secret"
    assert cfg.project == "already-set"      # the real environment wins
    assert cfg.database == "vcm"             # documented default


def test_missing_dotenv_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}
