"""FastAPI application exposing VCM features over REST, plus a static dashboard."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from vcm_ai import analyze, to_markdown
from vcm_core import Repository, VcmError
from vcm_diff import diff_revisions, render_text
from vcm_graph import build_graph
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
    by: str


def create_app(repo_path: str | None = None) -> FastAPI:
    """One app serves one repository (``repo_path`` or $VCM_REPO or the current directory)."""
    path = repo_path or os.environ.get("VCM_REPO") or "."
    app = FastAPI(title="VCM Hardware API", version="0.1.0")

    def repo() -> Repository:
        return Repository.discover(path)

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

    @app.get("/api/prs")
    def prs():
        return list_prs(repo())

    @app.post("/api/prs", status_code=201)
    def pr_open(body: PrIn):
        return open_pr(repo(), body.base, body.head, body.title, body.author)

    @app.get("/api/prs/{pr_id}")
    def pr_get(pr_id: int):
        return get_pr(repo(), pr_id)

    @app.post("/api/prs/{pr_id}/comments")
    def pr_comment(pr_id: int, body: CommentIn):
        return add_comment(repo(), pr_id, body.author, body.text)

    @app.post("/api/prs/{pr_id}/approvals")
    def pr_approve(pr_id: int, body: ApprovalIn):
        return approve(repo(), pr_id, body.by)

    @app.get("/")
    def dashboard():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
