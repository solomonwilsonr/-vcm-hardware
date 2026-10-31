"""FastAPI application exposing VCM features over REST, plus a static dashboard.

Authentication fails closed: the app refuses to start unless at least one API token exists
(``vcm token create`` or ``$VCM_API_TOKEN``), or ``insecure=True`` / ``$VCM_API_INSECURE=1`` is set
explicitly. With tokens, every author and approver is the identity behind the token, not a name
typed into a request body.
"""
from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vcm_ai import analyze, to_markdown
from vcm_core import Repository, VcmError
from vcm_core.tokens import TokenStore, role_at_least
from vcm_diff import diff_revisions, render_text
from vcm_graph import build_graph
from vcm_mcp import approve as approve_proposal
from vcm_mcp import get_proposal, list_proposals
from vcm_mcp import reject as reject_proposal
from vcm_trace import (
    affected_by_component,
    affected_by_revision,
    fleet_query,
    list_devices,
    list_racks,
    passport,
    plan_rollout,
    qr_svg,
    rack_power,
    recall,
)
from vcm_trace import register_device as _register_device
from vcm_validation import compare_validation, validate_revision
from vcm_workflow import (
    add_comment,
    approve,
    create_release,
    evaluate_policy,
    explain,
    get_pr,
    list_prs,
    load_policy,
    open_pr,
    plan_rollback,
    timeline,
    verify_release,
)

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
    author: str = "unknown"


class CommentIn(BaseModel):
    author: str = "unknown"
    text: str


class ApprovalIn(BaseModel):
    by: str = ""


class RejectIn(BaseModel):
    reason: str = ""


#: paths anyone may fetch: liveness, and the dashboard shell (which holds no repository data)
_OPEN = ("/api/health",)


def required_role(method: str, path: str) -> str | None:
    """Role a request needs, or None when the path is public."""
    if path in _OPEN or not path.startswith("/api/"):
        return None
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer"
    if path.startswith("/api/prs/") and path.endswith("/approvals"):
        return "approver"
    if path.startswith("/api/proposals/") and path.endswith(("/approve", "/reject")):
        return "approver"
    return "editor"


class AuthConfigError(RuntimeError):
    """Raised at startup when no authentication is configured and insecure mode was not asked for."""


def create_app(repo_path: str | None = None, insecure: bool | None = None) -> FastAPI:
    """One app serves one repository (``repo_path`` or $VCM_REPO or the current directory)."""
    path = repo_path or os.environ.get("VCM_REPO") or "."
    if insecure is None:
        insecure = os.environ.get("VCM_API_INSECURE", "") == "1"
    env_token = os.environ.get("VCM_API_TOKEN", "")
    env_user = os.environ.get("VCM_API_USER", "api-admin")
    store = TokenStore(Repository.discover(path).dir)
    if not insecure and not env_token and not store.configured():
        raise AuthConfigError(
            "refusing to start without authentication: create a token with "
            "`vcm token create USER --role ROLE`, set VCM_API_TOKEN, or pass --insecure "
            "explicitly for a trusted local network")
    app = FastAPI(title="VCM Hardware API", version="0.1.0")
    app.state.insecure = insecure

    def repo() -> Repository:
        return Repository.discover(path)

    def identify(token: str) -> dict | None:
        if env_token and hmac.compare_digest(token, env_token):
            return {"user": env_user, "role": "admin", "id": "env"}
        return store.identify(token) if token else None

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        request.state.identity = None
        needed = required_role(request.method, request.url.path)
        if needed is None or insecure:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        who = identify(token)
        if who is None:
            return JSONResponse({"detail": "authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        if not role_at_least(who["role"], needed):
            return JSONResponse({"detail": f"{who['user']} is {who['role']}; this needs {needed}"},
                                status_code=403)
        request.state.identity = who
        return await call_next(request)

    # The dashboard is a static bundle that fetches nothing off this origin, so a strict policy
    # costs nothing: no third-party script or frame can run in it, and it cannot be framed.
    # style-src keeps 'unsafe-inline' because the dashboard sets inline style attributes.
    _SECURITY_HEADERS = {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'"),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    def actor(request: Request, claimed: str) -> str:
        """The authenticated user; a self-asserted name only in insecure mode."""
        who = request.state.identity
        return who["user"] if who else (claimed or "unknown")

    @app.exception_handler(VcmError)
    async def _vcm_error(_: Request, exc: VcmError):
        missing = str(exc).startswith(("unknown", "no such", "no commits", "not a vcm", "not inside"))
        return JSONResponse({"detail": str(exc)}, status_code=404 if missing else 400)

    @app.exception_handler(KeyError)
    async def _key_error(_: Request, exc: KeyError):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.exception_handler(RecursionError)
    async def _too_deep(_: Request, exc: RecursionError):
        # a body nested thousands of levels deep exhausts the JSON parser; that is the client's
        # fault, not a server fault
        return JSONResponse({"detail": "request body is nested too deeply"}, status_code=400)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/whoami")
    def whoami(request: Request):
        who = request.state.identity
        return {"authenticated": who is not None, "insecure": insecure,
                "user": who["user"] if who else None, "role": who["role"] if who else None}

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

    @app.get("/api/policy")
    def policy(a: str, b: str):
        r = repo()
        pol = load_policy(r, b)
        report = analyze(r, a, b) if pol else None
        return evaluate_policy(pol, report, "check",
                               observed_base=r.latest_evidence(r.resolve(a), "observed") is not None)

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
    def release_verify(name: str, worktree: bool = False):
        return verify_release(repo(), name, worktree=worktree)

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

    @app.get("/api/racks")
    def racks():
        return list_racks(repo())

    def _criteria(type: str | None, model: str | None, mpn: str | None,
                  firmware: list[str] | None, rack: str | None, release: str | None) -> dict:
        return {"type_": type, "model": model, "mpn": mpn, "firmware": firmware,
                "rack": rack, "release": release}

    @app.get("/api/fleet/query")
    def fleet_query_(type: str | None = None, model: str | None = None, mpn: str | None = None,
                     firmware: Annotated[list[str] | None, Query()] = None, rack: str | None = None,
                     release: str | None = None):
        return fleet_query(repo(), **_criteria(type, model, mpn, firmware, rack, release))

    @app.get("/api/fleet/recall")
    def fleet_recall(type: str | None = None, model: str | None = None, mpn: str | None = None,
                     firmware: Annotated[list[str] | None, Query()] = None, rack: str | None = None,
                     release: str | None = None):
        if not any((type, model, mpn, firmware)):
            raise HTTPException(422, "a recall needs type, model, mpn or firmware")
        return recall(repo(), **_criteria(type, model, mpn, firmware, rack, release))

    @app.get("/api/fleet/power")
    def fleet_power(upgrade_to: str | None = None, release: str | None = None):
        return rack_power(repo(), upgrade_to, release)

    @app.get("/api/fleet/rollout")
    def fleet_rollout(to: str, release: str | None = None, max_per_rack: int = 1,
                      wave_size: int = 10):
        return plan_rollout(repo(), to, release, max_per_rack, wave_size)

    @app.get("/api/proposals")
    def proposals(status: str | None = None):
        return list_proposals(repo(), status)

    @app.get("/api/proposals/{pid}")
    def proposal(pid: str):
        return get_proposal(repo(), pid)

    def _decider(request: Request) -> str:
        who = request.state.identity
        if who is None:  # insecure mode has no identity, so nothing here may be decided
            raise HTTPException(403, "proposals can only be decided by an authenticated person")
        return who["user"]

    @app.post("/api/proposals/{pid}/approve")
    def proposal_approve(pid: str, request: Request):
        return approve_proposal(repo(), pid, _decider(request))

    @app.post("/api/proposals/{pid}/reject")
    def proposal_reject(pid: str, body: RejectIn, request: Request):
        return reject_proposal(repo(), pid, _decider(request), body.reason)

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
        by = actor(request, body.by)
        if not by or by == "unknown":
            raise HTTPException(422, "approver name required")
        if request.state.identity and get_pr(repo(), pr_id)["author"] == by:
            raise HTTPException(403, "authors cannot approve their own change")
        return approve(repo(), pr_id, by)

    @app.get("/")
    def dashboard():
        return FileResponse(STATIC / "index.html")

    # The dashboard's stylesheet and script. Served from disk so the page stays a
    # plain static bundle: no build step, no CDN, nothing fetched off this origin.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    return app

