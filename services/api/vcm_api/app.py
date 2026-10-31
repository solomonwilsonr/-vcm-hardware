"""FastAPI application exposing VCM features over REST, plus a static dashboard."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vcm_ai import analyze, to_markdown
from vcm_core import Repository, VcmError
from vcm_core.signing import load_trustdb
from vcm_diff import diff_revisions, render_text
from vcm_graph import build_graph
from vcm_graph.backends.appwrite_records import realtime_config
from vcm_trace import (
    affected_by_component,
    affected_by_revision,
    list_devices,
    passport,
    qr_svg,
)
from vcm_trace import register_device as _register_device
from vcm_validation import compare_validation, validate_revision
from vcm_workflow import (
    add_comment,
    approve,
    create_release,
    explain,
    get_pr,
    list_prs,
    merge_pr,
    open_pr,
    plan_rollback,
    sign_release,
    timeline,
    verify_release,
)

from .auth import PUBLIC, allows, identity_for, normalize_tokens, required_role, tokens_from_env

STATIC = Path(__file__).parent / "static"


class ReleaseIn(BaseModel):
    name: str
    rev: str = "HEAD"


class DeviceIn(BaseModel):
    serial: str
    release: str
    batch: str = ""
    notes: str = ""


class PrIn(BaseModel):
    base: str
    head: str
    title: str
    author: str | None = None


class CommentIn(BaseModel):
    author: str | None = None
    text: str


class ApprovalIn(BaseModel):
    by: str | None = None


class SignIn(BaseModel):
    key: str


class LockIn(BaseModel):
    path: str
    ttl: int = 8 * 3600
    note: str = ""


def create_app(repo_path: str | None = None, *, tokens: dict[str, str] | None = None,
               insecure: bool = False) -> FastAPI:
    """One app serves one repository (``repo_path`` or $VCM_REPO or the current directory).

    Fails closed: with no ``tokens`` (token -> identity; default from the environment) it raises
    unless ``insecure`` is set. With auth on, every ``/api`` route except health needs a bearer
    token, and the acting identity is the token's, not anything in the request body. Tokens map to
    an identity and a role (reader < writer < maintainer); see ``auth.py``.
    """
    path = repo_path or os.environ.get("VCM_REPO") or "."
    tokens = tokens_from_env() if tokens is None else normalize_tokens(tokens)
    if not tokens and not insecure:
        raise VcmError("refusing to start without authentication: set VCM_API_TOKEN (or "
                       "VCM_API_TOKENS), or pass insecure=True / --insecure to opt out")
    app = FastAPI(title="VCM Hardware API", version="0.1.0")

    def repo() -> Repository:
        return Repository.discover(path)

    def actor(request: Request, claimed: str | None) -> str:
        """Authenticated identity if auth is on; otherwise the self-asserted name."""
        return getattr(request.state, "identity", None) or claimed or "unknown"

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        request.state.identity = request.state.role = None
        if tokens and request.url.path.startswith("/api") and request.url.path not in PUBLIC:
            found = identity_for(request.headers.get("authorization"), tokens)
            if found is None:
                return JSONResponse({"detail": "missing or invalid bearer token"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            request.state.identity, request.state.role = found
            needed = required_role(request.method, request.url.path, dict(request.query_params))
            if not allows(request.state.role, needed):
                return JSONResponse({"detail": f"{request.state.identity} is a "
                                     f"{request.state.role}; this needs {needed}"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(VcmError)
    async def _vcm_error(_: Request, exc: VcmError):
        missing = str(exc).startswith(("unknown", "no such", "no commits", "not a vcm", "not inside"))
        return JSONResponse({"detail": str(exc)}, status_code=404 if missing else 400)

    @app.exception_handler(KeyError)
    async def _key_error(_: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/store")
    def store():
        be = repo().records
        return {"backend": be.name, "scope": getattr(be, "scope", None),
                "realtime": realtime_config(scope=getattr(be, "scope", None))}

    @app.get("/api/events")
    def events(after: str = ""):
        """What changed since ``after`` (kind, name, action, actor; never content). The dashboard
        polls this, or uses it as the cue to refetch when Appwrite Realtime fires."""
        evs = repo().records.events(after)
        return {"events": evs, "cursor": evs[-1]["at"] if evs else after}

    @app.get("/api/whoami")
    def whoami(request: Request):
        return {"identity": request.state.identity, "role": request.state.role,
                "authenticated": bool(tokens)}

    @app.get("/api/repo")
    def repo_info():
        r = repo()
        return {"path": str(r.root), "branch": r.current_branch, "head": r.head_id(),
                "branches": r.branches(), "tags": r.tags()}

    @app.get("/api/revisions")
    def revisions():
        return timeline(repo())

    @app.get("/api/timeline")
    def timeline_(start: str = "HEAD"):
        return timeline(repo(), start)

    @app.get("/api/revisions/{rev}")
    def revision(rev: str):
        r = repo().get_revision(rev)
        return {**r.summary(), "artifacts": r.body()["artifacts"]}

    @app.get("/api/diff")
    def diff(a: str, b: str, format: str = Query("json", pattern="^(json|text)$")):
        d = diff_revisions(repo(), a, b)
        return PlainTextResponse(render_text(d)) if format == "text" else d

    @app.get("/api/impact")
    def impact(a: str, b: str, format: str = Query("json", pattern="^(json|markdown)$")):
        rep = analyze(repo(), a, b)
        return PlainTextResponse(to_markdown(rep), media_type="text/markdown") if format == "markdown" else rep

    @app.get("/api/explain")
    def explain_(a: str, b: str):
        return explain(repo(), a, b)

    @app.get("/api/graph/affected")
    def graph_affected(component: str, rev: str = "HEAD", depth: int = 1):
        return build_graph(repo(), rev).affected_by(component, depth)

    @app.get("/api/graph/stats")
    def graph_stats(rev: str = "HEAD"):
        return build_graph(repo(), rev).stats()

    @app.get("/api/validation/compare")
    def validation_compare(a: str, b: str):
        return compare_validation(repo(), a, b)

    @app.get("/api/validation/{rev}")
    def validation_get(rev: str):
        res = repo().latest_evidence(rev, "validation")
        if res is None:
            raise HTTPException(404, "no validation evidence for this revision")
        return res

    @app.post("/api/validation/{rev}")
    def validation_run(rev: str, kicad: bool = False):
        return validate_revision(repo(), rev, run_kicad=kicad)

    @app.get("/api/releases")
    def releases():
        return repo().list_records("releases")

    @app.post("/api/releases", status_code=201)
    def release_create(body: ReleaseIn):
        return create_release(repo(), body.name, body.rev)

    @app.get("/api/releases/{name}")
    def release_get(name: str):
        return repo().load_record("releases", name)

    @app.get("/api/releases/{name}/verify")
    def release_verify(name: str, worktree: bool = False, require_signature: bool = False):
        return verify_release(repo(), name, worktree=worktree, require_signature=require_signature)

    @app.post("/api/releases/{name}/signatures", status_code=201)
    def release_sign(name: str, body: SignIn):
        return sign_release(repo(), name, body.key)

    @app.get("/api/trust")
    def trust():
        return load_trustdb(repo().root)

    @app.get("/api/rollback")
    def rollback(target: str, current: str = "HEAD"):
        return plan_rollback(repo(), target, current)

    @app.get("/api/devices")
    def devices():
        return list_devices(repo())

    @app.post("/api/devices", status_code=201)
    def device_register(body: DeviceIn):
        return _register_device(repo(), body.serial, body.release, body.batch, body.notes)

    @app.get("/api/devices/affected")
    def devices_affected(component: str | None = None, value: str | None = None,
                         revision: str | None = None):
        if component:
            return affected_by_component(repo(), component, value)
        if revision:
            return affected_by_revision(repo(), revision)
        raise HTTPException(422, "give component or revision")

    @app.get("/api/devices/{serial}/passport")
    def device_passport(serial: str):
        return passport(repo(), serial)

    @app.get("/api/devices/{serial}/qr.svg")
    def device_qr(serial: str):
        dev = repo().load_record("devices", serial)
        return Response(qr_svg(dev["qr_payload"]), media_type="image/svg+xml")

    @app.get("/api/prs")
    def prs():
        return list_prs(repo())

    @app.post("/api/prs", status_code=201)
    def pr_open(body: PrIn, request: Request):
        return open_pr(repo(), body.base, body.head, body.title, actor(request, body.author))

    @app.get("/api/prs/{pr_id}")
    def pr_get(pr_id: int):
        return get_pr(repo(), pr_id)

    @app.post("/api/prs/{pr_id}/comments")
    def pr_comment(pr_id: int, body: CommentIn, request: Request):
        return add_comment(repo(), pr_id, actor(request, body.author), body.text)

    @app.post("/api/prs/{pr_id}/approvals")
    def pr_approve(pr_id: int, body: ApprovalIn, request: Request):
        return approve(repo(), pr_id, actor(request, body.by))

    @app.post("/api/prs/{pr_id}/merge")
    def pr_merge(pr_id: int, request: Request):
        who = request.state.identity
        if tokens and not who:  # unreachable behind the middleware; guards against a regression
            raise HTTPException(401, "authentication required")
        return merge_pr(repo(), pr_id, who or request.query_params.get("by") or "unknown")

    @app.get("/api/locks")
    def locks_list():
        return repo().locks.list()

    @app.post("/api/locks", status_code=201)
    def lock_acquire(body: LockIn, request: Request):
        return repo().locks.acquire(body.path, actor(request, request.query_params.get("by")),
                                    body.ttl, body.note)

    @app.delete("/api/locks")
    def lock_release(path: str, request: Request, force: bool = False):
        return repo().locks.release(path, actor(request, request.query_params.get("by")), force)

    @app.get("/")
    def dashboard():
        return FileResponse(STATIC / "index.html")

    # The dashboard's stylesheet and script. Served from disk so the page stays a
    # plain static bundle: no build step, no CDN, nothing fetched off this origin.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    return app

