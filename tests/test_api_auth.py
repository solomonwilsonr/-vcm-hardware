import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from vcm_api import create_app  # noqa: E402
from vcm_api.app import AuthConfigError, required_role  # noqa: E402
from vcm_core.tokens import TokenStore  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("VCM_API_TOKEN", "VCM_API_INSECURE", "VCM_API_USER"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def tokens(sample_repo):
    store = TokenStore(sample_repo.dir)
    return {r: store.create(f"{r}-user", r, "2026-01-01T00:00:00Z")
            for r in ("viewer", "editor", "approver")}


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


def test_fails_closed_without_tokens(sample_repo):
    with pytest.raises(AuthConfigError, match="refusing to start"):
        create_app(str(sample_repo.root))
    create_app(str(sample_repo.root), insecure=True)  # explicit opt-out still works


def test_env_token_and_insecure_env(sample_repo, monkeypatch):
    monkeypatch.setenv("VCM_API_TOKEN", "s3cret")
    c = TestClient(create_app(str(sample_repo.root)))
    assert c.get("/api/repo").status_code == 401
    assert c.get("/api/repo", headers=_auth("wrong")).status_code == 401
    who = c.get("/api/whoami", headers=_auth("s3cret")).json()
    assert who == {"authenticated": True, "insecure": False, "user": "api-admin", "role": "admin"}


def test_roles_are_enforced(sample_repo, tokens):
    c = TestClient(create_app(str(sample_repo.root)))
    assert c.get("/api/health").status_code == 200        # liveness stays public
    assert c.get("/").status_code == 200                  # the dashboard shell holds no data
    assert c.get("/api/revisions").status_code == 401
    assert c.get("/api/revisions", headers=_auth(tokens["viewer"])).status_code == 200
    r = c.post("/api/releases", json={"name": "R1", "rev": "v1.1"},
               headers=_auth(tokens["viewer"]))
    assert r.status_code == 403 and "needs editor" in r.json()["detail"]
    assert c.post("/api/releases", json={"name": "R1", "rev": "v1.1"},
                  headers=_auth(tokens["editor"])).status_code == 201


def test_identity_replaces_self_asserted_names(sample_repo, tokens):
    c = TestClient(create_app(str(sample_repo.root)))
    pr = c.post("/api/prs", json={"base": "v1.0", "head": "v1.1", "title": "swap",
                                  "author": "someone-else"},
                headers=_auth(tokens["editor"])).json()
    assert pr["author"] == "editor-user"
    pid = pr["id"]
    c.post(f"/api/prs/{pid}/comments", json={"author": "ceo", "text": "lgtm"},
           headers=_auth(tokens["viewer"]))  # viewers cannot comment
    got = c.get(f"/api/prs/{pid}", headers=_auth(tokens["viewer"])).json()
    assert got["comments"] == []
    c.post(f"/api/prs/{pid}/comments", json={"author": "ceo", "text": "lgtm"},
           headers=_auth(tokens["editor"]))
    got = c.get(f"/api/prs/{pid}", headers=_auth(tokens["viewer"])).json()
    assert got["comments"][0]["author"] == "editor-user"

    # editors cannot approve; approvers approve as themselves, whatever the body claims
    assert c.post(f"/api/prs/{pid}/approvals", json={"by": "x"},
                  headers=_auth(tokens["editor"])).status_code == 403
    ok = c.post(f"/api/prs/{pid}/approvals", json={"by": "ceo"},
                headers=_auth(tokens["approver"])).json()
    assert [a["by"] for a in ok["approvals"]] == ["approver-user"]


def test_self_approval_refused(sample_repo):
    store = TokenStore(sample_repo.dir)
    t = store.create("dana", "approver", "2026-01-01T00:00:00Z")
    c = TestClient(create_app(str(sample_repo.root)))
    pid = c.post("/api/prs", json={"base": "v1.0", "head": "v1.1", "title": "x"},
                 headers=_auth(t)).json()["id"]
    r = c.post(f"/api/prs/{pid}/approvals", json={}, headers=_auth(t))
    assert r.status_code == 403 and "own change" in r.json()["detail"]


def test_revoked_token_stops_working(sample_repo, tokens):
    c = TestClient(create_app(str(sample_repo.root)))
    store = TokenStore(sample_repo.dir)
    tid = next(t["id"] for t in store.list() if t["user"] == "viewer-user")
    assert store.revoke(tid) == 1
    assert c.get("/api/repo", headers=_auth(tokens["viewer"])).status_code == 401


def test_tokens_stored_hashed(sample_repo, tokens):
    text = (sample_repo.dir / "tokens.json").read_text()
    assert all(t not in text for t in tokens.values())
    assert (sample_repo.dir / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_required_role_table():
    assert required_role("GET", "/api/health") is None
    assert required_role("GET", "/static/app.js") is None
    assert required_role("GET", "/api/impact") == "viewer"
    assert required_role("POST", "/api/devices") == "editor"
    assert required_role("POST", "/api/prs/3/approvals") == "approver"
