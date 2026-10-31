import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from vcm_api import create_app  # noqa: E402


@pytest.fixture
def client(sample_repo):
    return TestClient(create_app(str(sample_repo.root), insecure=True))


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


def test_api_rejects_path_traversal(client):
    r = client.get("/api/releases/..%2Fsecret")
    assert r.status_code in (400, 404)


def test_dashboard_assets_are_served(client):
    """The dashboard is three static files; it must not need a build step."""
    page = client.get("/").text
    assert '<link rel="stylesheet" href="/static/app.css">' in page
    assert '<script src="/static/app.js"></script>' in page
    css = client.get("/static/app.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200 and "text/css" in css.headers["content-type"]
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]


def test_dashboard_is_fully_offline(client):
    """Hard rule: no network at runtime. Nothing may be fetched off this origin."""
    blobs = [client.get(p).text for p in ("/", "/static/app.css", "/static/app.js")]
    for text in blobs:
        for scheme in ("http://", "https://", "//cdn", "@import url("):
            # http(s) URLs may only appear as prose or sample payloads, never as a
            # src/href/url() that the browser would actually fetch.
            for attr in ('src="', "src='", 'href="', "href='", "url("):
                assert attr + scheme not in text, (attr, scheme)
    assert "fonts.googleapis" not in "".join(blobs)


def test_dashboard_encodes_confidence_classes_without_colour_alone(client):
    """Each confidence class needs a text label and a non-colour cue."""
    css = client.get("/static/app.css").text
    js = client.get("/static/app.js").text
    for cls in ("verified", "rule_based", "speculative"):
        assert "." + cls in css
        assert cls in js
    # Non-colour cue 1: distinct border style per class.
    assert ".cchip.verified" in css and "border-style: solid" in css
    assert ".cchip.rule_based" in css and "border-style: dashed" in css
    assert ".cchip.speculative" in css and "border-style: dotted" in css
    # Non-colour cue 2: a distinct SVG shape per class, drawn by cmark().
    assert "function cmark(" in js
    # Severity is a separate axis with its own chip and glyphs.
    assert ".schip.critical" in css and ".schip.warning" in css and ".schip.info" in css
    assert "function schip(" in js


def test_dashboard_vocabulary_is_domain_neutral(client):
    """The UI must read its vocabulary from the data, not assume PCB design.

    Payload *keys* (``footprint_changes``, ``nets_added``) are part of the API
    contract and are allowed; user-visible labels and hard-coded reference
    designators are not.
    """
    js = client.get("/static/app.js").text
    labels = [line for line in js.splitlines() if '"' in line]
    banned = ("Footprint changes", "Net\"", "Nets\"", "BOM", "Schematic", "Board",
              "Placement\"", "Electrically connected", "Δ mm")
    for word in banned:
        assert word not in js, word
    # No hard-coded reference designator: the example comes from the timeline.
    assert 'ref: null' in js and "function exampleRef(" in js
    assert '"U1"' not in js and '"C2"' not in js
    assert labels  # sanity: we actually scanned something


def test_dashboard_surfaces_catalog_coverage(client):
    """Catalog coverage is a domain-neutral trust signal and must be shown."""
    rep = client.get("/api/impact", params={"a": "v1.0", "b": "v1.1"}).json()
    assert "catalog" in rep, "impact report no longer carries a catalog block"
    js = client.get("/static/app.js").text
    assert "function catalogPanel(" in js
    assert "catalogPanel(rep.catalog)" in js


def test_static_mount_rejects_traversal(client):
    assert client.get("/static/../app.py").status_code in (301, 307, 400, 404)


def test_dashboard_ships_the_fleet_view(client):
    page = client.get("/").text
    assert 'href="#/fleet"' in page
    js = client.get("/static/app.js").text
    for endpoint in ("/api/fleet/power", "/api/fleet/rollout", "/api/fleet/query"):
        assert endpoint in js
    assert "fleet: fleetView" in js


def test_dashboard_has_an_agents_page_that_cannot_propose(client):
    """Agents propose, people decide: the page lists proposals and forwards a decision, and the
    dashboard has no code path that creates a proposal."""
    page = client.get("/").text
    js = client.get("/static/app.js").text
    assert 'data-view="agents"' in page
    assert "function agentsView(" in js and '"/api/proposals"' in js
    assert '"/approve"' in js or '"approve"' in js
    # nothing in the dashboard posts a new proposal
    assert 'api("/api/proposals", {' not in js and "/api/proposals\", { json" not in js
