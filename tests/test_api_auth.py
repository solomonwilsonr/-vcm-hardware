import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from vcm_api import create_app  # noqa: E402
from vcm_api.auth import identity_for, tokens_from_env  # noqa: E402
from vcm_core import VcmError  # noqa: E402

ALICE, BOB = "alice-token-0123456789", "bob-token-0123456789"


@pytest.fixture
def client(sample_repo):
    app = create_app(str(sample_repo.root), tokens={ALICE: "alice", BOB: "bob"})
    return TestClient(app)


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_fails_closed_without_tokens(sample_repo, monkeypatch):
    monkeypatch.delenv("VCM_API_TOKEN", raising=False)
    monkeypatch.delenv("VCM_API_TOKENS", raising=False)
    with pytest.raises(VcmError, match="refusing to start"):
        create_app(str(sample_repo.root))
    assert create_app(str(sample_repo.root), insecure=True)  # explicit opt-out works


def test_requests_need_a_valid_token(client):
    assert client.get("/api/health").status_code == 200  # liveness stays public
    r = client.get("/api/repo")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/repo", headers=auth("wrong-token-0123456789")).status_code == 401
    assert client.get("/api/repo", headers={"Authorization": ALICE}).status_code == 401
    assert client.get("/api/repo", headers=auth(ALICE)).status_code == 200
    assert client.post("/api/releases", json={"name": "R", "rev": "v1.0"}).status_code == 401
    assert client.get("/api/whoami", headers=auth(BOB)).json() == {
        "identity": "bob", "role": "writer", "authenticated": True}
    assert client.get("/").status_code == 200  # the static dashboard shell holds no data


def test_identity_comes_from_the_token_not_the_body(client):
    pr = client.post("/api/prs", headers=auth(ALICE), json={
        "base": "v1.0", "head": "v1.1", "title": "x", "author": "mallory"}).json()
    assert pr["author"] == "alice"
    client.post("/api/prs/1/comments", headers=auth(BOB), json={"author": "alice", "text": "hi"})
    assert client.get("/api/prs/1", headers=auth(BOB)).json()["comments"][0]["author"] == "bob"
    # alice cannot approve her own PR, even by claiming to be bob
    r = client.post("/api/prs/1/approvals", headers=auth(ALICE), json={"by": "bob"})
    assert r.status_code == 400 and "own pull request" in r.json()["detail"]
    ok = client.post("/api/prs/1/approvals", headers=auth(BOB), json={"by": "alice"}).json()
    assert [a["by"] for a in ok["approvals"]] == ["bob"]


def test_merge_and_locks_use_authenticated_identity(sample_repo):
    r = sample_repo
    r.branch("feature", "v1.1")
    r.checkout("v1.0", force=True)
    r._set_ref("heads", "main", r.resolve("v1.0"))
    (r.dir / "HEAD").write_text("ref: refs/heads/main\n")
    c = TestClient(create_app(str(r.root), tokens={ALICE: ("alice", "maintainer"), BOB: "bob"}))
    c.post("/api/prs", headers=auth(ALICE), json={"base": "main", "head": "feature", "title": "x"})
    assert c.post("/api/prs/1/merge", headers=auth(BOB)).status_code == 403  # a writer cannot merge
    c.post("/api/prs/1/approvals", headers=auth(BOB), json={})
    resp = c.post("/api/prs/1/merge", headers=auth(ALICE))
    assert resp.status_code == 200, resp.text
    merged = resp.json()
    assert merged["state"] == "merged" and merged["merged_by"] == "alice"

    lock = c.post("/api/locks", headers=auth(ALICE), json={"path": "a.txt"})
    assert lock.status_code == 201 and lock.json()["holder"] == "alice"
    assert c.post("/api/locks", headers=auth(BOB), json={"path": "a.txt"}).status_code == 400
    assert c.delete("/api/locks", headers=auth(BOB), params={"path": "a.txt"}).status_code == 400
    forced = c.delete("/api/locks", headers=auth(BOB), params={"path": "a.txt", "force": "true"})
    assert forced.status_code == 403  # breaking someone else's lock needs maintainer
    assert c.delete("/api/locks", headers=auth(ALICE), params={"path": "a.txt"}).status_code == 200
    assert c.get("/api/locks", headers=auth(ALICE)).json() == []


def test_release_signing_over_api(sample_repo, tmp_path, monkeypatch):
    from vcm_core import signing
    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))
    c = TestClient(create_app(str(sample_repo.root), tokens={ALICE: ("alice", "maintainer")}))
    c.post("/api/releases", headers=auth(ALICE), json={"name": "R1", "rev": "v1.0"})
    info = signing.generate_key("alice")
    signing.trust_key(sample_repo.root, "alice", info["public"])
    assert c.post("/api/releases/R1/signatures", headers=auth(ALICE),
                  json={"key": "alice"}).status_code == 201
    res = c.get("/api/releases/R1/verify", headers=auth(ALICE),
                params={"require_signature": True}).json()
    assert res["ok"] and res["authenticated"]
    assert list(c.get("/api/trust", headers=auth(ALICE)).json()["keys"]) == [info["key_id"]]


def test_token_parsing():
    assert tokens_from_env({"VCM_API_TOKEN": "x" * 16, "VCM_API_USER": "ci"}) == {
        "x" * 16: ("ci", "maintainer")}
    assert tokens_from_env({"VCM_API_TOKENS": f"a={'1' * 16}:reader, b={'2' * 16}"}) == {
        "1" * 16: ("a", "reader"), "2" * 16: ("b", "writer")}
    with pytest.raises(VcmError, match="unknown role"):
        tokens_from_env({"VCM_API_TOKEN": "x" * 16, "VCM_API_ROLE": "root"})
    assert tokens_from_env({}) == {}
    with pytest.raises(VcmError, match="shorter"):
        tokens_from_env({"VCM_API_TOKEN": "short"})
    with pytest.raises(VcmError, match="alice=token"):
        tokens_from_env({"VCM_API_TOKENS": "just-a-token-with-no-name"})
    assert identity_for("Bearer " + "x" * 16, {"x" * 16: ("a", "writer")}) == ("a", "writer")
    assert identity_for("Basic abc", {"x" * 16: ("a", "writer")}) is None
    assert identity_for(None, {}) is None


def test_roles_gate_methods(sample_repo):
    tok = {"r" * 16: ("rita", "reader"), "w" * 16: ("will", "writer")}
    c = TestClient(create_app(str(sample_repo.root), tokens=tok))
    assert c.get("/api/repo", headers=auth("r" * 16)).status_code == 200
    denied = c.post("/api/releases", headers=auth("r" * 16), json={"name": "R", "rev": "v1.0"})
    assert denied.status_code == 403 and "needs writer" in denied.json()["detail"]
    assert c.post("/api/releases", headers=auth("w" * 16), json={"name": "R", "rev": "v1.0"}
                  ).status_code == 201
    signed = c.post("/api/releases/R/signatures", headers=auth("w" * 16), json={"key": "k"})
    assert signed.status_code == 403 and "needs maintainer" in signed.json()["detail"]
