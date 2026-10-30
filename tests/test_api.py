import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from vcm_api import create_app  # noqa: E402


@pytest.fixture
def client(sample_repo):
    return TestClient(create_app(str(sample_repo.root)))


def test_health_repo_revisions(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    info = client.get("/api/repo").json()
    assert info["branch"] == "main" and set(info["tags"]) == {"v1.0", "v1.1"}
    revs = client.get("/api/revisions").json()
    assert [r["tags"] for r in revs] == [["v1.1"], ["v1.0"]]
    r = client.get("/api/revisions/v1.1").json()
    assert r["artifact_count"] == len(r["artifacts"])
    assert client.get("/api/revisions/nope").status_code == 404


def test_diff_impact_graph(client):
    d = client.get("/api/diff", params={"a": "v1.0", "b": "v1.1"}).json()
    assert d["changed_refs"] == ["C2", "R1", "U1"]
    assert "AMS1117-3.3" in client.get("/api/diff", params={"a": "v1.0", "b": "v1.1", "format": "text"}).text
    rep = client.get("/api/impact", params={"a": "v1.0", "b": "v1.1"}).json()
    assert {f["confidence_class"] for f in rep["findings"]} == {"verified", "rule_based", "speculative"}
    md = client.get("/api/impact", params={"a": "v1.0", "b": "v1.1", "format": "markdown"})
    assert "## Speculative" in md.text
    g = client.get("/api/graph/affected", params={"component": "U1", "rev": "v1.1"}).json()
    assert "U2" in {c["ref"] for c in g["components"]}
    assert client.get("/api/graph/affected", params={"component": "ZZ"}).status_code == 404


def test_validation_release_rollback(client):
    assert client.get("/api/validation/v1.0").status_code == 404
    assert client.post("/api/validation/v1.0").json()["status"] == "pass"
    assert client.get("/api/validation/v1.0").json()["status"] == "pass"
    assert client.get("/api/validation/compare", params={"a": "v1.0", "b": "v1.1"}).json()["regression"] is False
    r = client.post("/api/releases", json={"name": "R1.1", "rev": "v1.1"})
    assert r.status_code == 201
    assert client.post("/api/releases", json={"name": "R1.1", "rev": "v1.1"}).status_code == 400
    assert client.get("/api/releases/R1.1/verify").json()["ok"] is True
    assert [x["name"] for x in client.get("/api/releases").json()] == ["R1.1"]
    plan = client.get("/api/rollback", params={"target": "v1.0", "current": "v1.1"}).json()
    assert plan["fully_software_reversible"] is False


def test_devices_and_passport(client):
    client.post("/api/releases", json={"name": "R1.0", "rev": "v1.0"})
    r = client.post("/api/devices", json={"serial": "SN-1", "release": "R1.0", "batch": "B"})
    assert r.status_code == 201
    assert client.post("/api/devices", json={"serial": "SN-1", "release": "R1.0"}).status_code == 400
    p = client.get("/api/devices/SN-1/passport").json()
    assert p["release"]["name"] == "R1.0" and p["lineage"]
    assert client.get("/api/devices/SN-1/qr.svg").headers["content-type"].startswith("image/svg")
    aff = client.get("/api/devices/affected", params={"component": "U1", "value": "AMS1117-3.3"}).json()
    assert [d["serial"] for d in aff] == ["SN-1"]
    by_rev = client.get("/api/devices/affected", params={"revision": "v1.0"}).json()
    assert [d["serial"] for d in by_rev["devices"]] == ["SN-1"]
    assert client.get("/api/devices/affected").status_code == 422
    assert [d["serial"] for d in client.get("/api/devices").json()] == ["SN-1"]


def test_pull_requests(client):
    pr = client.post("/api/prs", json={"base": "v1.0", "head": "v1.1", "title": "LDO swap"}).json()
    assert pr["id"] == 1 and pr["status"]["critical_findings"]
    client.post("/api/prs/1/comments", json={"author": "a", "text": "hmm"})
    pr = client.post("/api/prs/1/approvals", json={"by": "alice"}).json()
    assert pr["status"]["ready_for_merge"] and pr["comments"][0]["text"] == "hmm"
    assert client.get("/api/prs/99").status_code == 404
    assert client.get("/api/prs").json()[0]["approvals"] == 1


def test_timeline_explain_and_dashboard(client):
    assert len(client.get("/api/timeline").json()) == 2
    assert "What changed" in client.get("/api/explain", params={"a": "v1.0", "b": "v1.1"}).json()["text"]
    page = client.get("/")
    assert page.status_code == 200 and "Every hardware revision has a story" in page.text
