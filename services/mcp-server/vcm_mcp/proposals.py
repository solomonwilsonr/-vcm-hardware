"""Proposals: the only way an agent changes anything, and only after a human approves.

An agent calls ``vcm_propose``; that writes a pending record and nothing else. A person then runs
``vcm proposal approve`` (or uses the API with an approver token). Approval re-checks the action
against the repository as it is *now*, then performs it as the approving person, so the audit log
names the human who allowed it and the agent that asked.

Deliberately not proposable, because each would let an agent manufacture trust:

* signing a release or a passport;
* ``catalog review`` (it records that a human checked a datasheet);
* overriding a policy violation;
* committing, tagging, or touching remotes and locks.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager

from vcm_core import Repository, VcmError, now_iso
from vcm_trace import define_rack, place_device, register_device
from vcm_trace.devices import SERIAL_RE
from vcm_workflow import create_release, open_pr
from vcm_workflow.release import NAME_RE, release_policy

KIND = "proposals"
MAX_PENDING_PER_PROPOSER = 50
AGENT_PREFIX = "agent:"
_ID = re.compile(r"^P\d{4,}$")

#: action -> (required fields, optional fields, what approving it does)
ACTIONS: dict[str, dict] = {
    "release.create": {
        "required": {"name": str, "rev": str}, "optional": {},
        "summary": "Pin a revision as a release (the committed policy is enforced; no override)."},
    "pr.open": {
        "required": {"base": str, "head": str, "title": str}, "optional": {},
        "summary": "Open a hardware review between two revisions."},
    "fleet.place": {
        "required": {"serial": str, "rack": str}, "optional": {"position": int},
        "summary": "Record which rack a registered unit is installed in."},
    "rack.define": {
        "required": {"name": str},
        "optional": {"power_budget_w": (int, float), "site": str, "units": int},
        "summary": "Create or update a rack and its power budget."},
    "device.register": {
        "required": {"serial": str, "release": str}, "optional": {"batch": str, "notes": str},
        "summary": "Register a physical unit against a release."},
}


def proposer_name() -> str:
    return AGENT_PREFIX + (os.environ.get("VCM_AGENT_NAME", "").strip() or "mcp-agent")


def _check_shape(action: str, args: dict) -> dict:
    spec = ACTIONS.get(action)
    if spec is None:
        raise VcmError(f"unknown action {action!r}; proposable actions: "
                       f"{', '.join(sorted(ACTIONS))}")
    if not isinstance(args, dict):
        raise VcmError("args must be an object")
    allowed = {**spec["required"], **spec["optional"]}
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise VcmError(f"{action}: unexpected argument(s) {', '.join(extra)}")
    missing = sorted(set(spec["required"]) - set(args))
    if missing:
        raise VcmError(f"{action}: missing argument(s) {', '.join(missing)}")
    for key, value in args.items():
        want = allowed[key]
        if isinstance(value, bool) or not isinstance(value, want):
            raise VcmError(f"{action}: {key} has the wrong type")
        if isinstance(value, str) and (not value.strip() or len(value) > 500):
            raise VcmError(f"{action}: {key} must be a non-empty string under 500 characters")
    return dict(args)


def precheck(repo: Repository, action: str, args: dict) -> tuple[dict, dict]:
    """Validate against the repository as it is now; return (resolved args, preview).

    Revisions are resolved to ids here, so approving later acts on exactly what was reviewed.
    """
    a = _check_shape(action, args)
    if action == "release.create":
        if not NAME_RE.match(a["name"]):
            raise VcmError(f"invalid release name: {a['name']!r}")
        if (repo.dir / "releases" / f"{a['name']}.json").exists():
            raise VcmError(f"release already exists: {a['name']}")
        a["rev"] = repo.resolve(a["rev"])
        pol = release_policy(repo, repo.get_revision(a["rev"]))
        if not pol["ok"]:
            raise VcmError("this release would be blocked by vcm-policy.json: " + "; ".join(
                f"{v['id']}: {', '.join(v['problems'])}" for v in pol["violations"])
                + ". Overrides are a human decision and cannot be proposed.")
        return a, {"policy": {"present": pol["policy"], "ok": pol["ok"],
                              "skipped": [s["id"] for s in pol["skipped"]]}}
    if action == "pr.open":
        a["base"], a["head"] = repo.resolve(a["base"]), repo.resolve(a["head"])
        if a["base"] == a["head"]:
            raise VcmError("base and head are the same revision")
        if len(a["title"]) > 200:
            raise VcmError("title is over 200 characters")
        return a, {}
    if action == "fleet.place":
        repo.load_record("devices", a["serial"])  # raises if the unit is unknown
        if a.get("position", 0) < 0:
            raise VcmError("position must be 0 or more")
        known = any(r["name"] == a["rack"] for r in repo.list_records("racks"))
        return a, {"rack_exists": known,
                   "note": None if known else "the rack does not exist and will be created"}
    if action == "rack.define":
        if not SERIAL_RE.match(a["name"]):
            raise VcmError(f"invalid rack name: {a['name']!r}")
        if a.get("power_budget_w", 1) <= 0 or a.get("units", 1) <= 0:
            raise VcmError("power_budget_w and units must be positive")
        return a, {"updates_existing": (repo.dir / "racks" / f"{a['name']}.json").exists()}
    # device.register
    if not SERIAL_RE.match(a["serial"]):
        raise VcmError(f"invalid serial: {a['serial']!r}")
    if (repo.dir / "devices" / f"{a['serial']}.json").exists():
        raise VcmError(f"device already registered: {a['serial']}")
    rel = repo.load_record("releases", a["release"])
    return a, {"revision": rel["revision"]}


def _next_id(repo: Repository) -> str:
    n = max((int(p["id"][1:]) for p in repo.list_records(KIND) if _ID.match(p["id"])), default=0)
    return f"P{n + 1:04d}"


def propose(repo: Repository, action: str, args: dict, rationale: str,
            proposer: str | None = None) -> dict:
    proposer = proposer or proposer_name()
    if not isinstance(rationale, str) or not rationale.strip():
        raise VcmError("a proposal needs a rationale a person can read")
    pending = [p for p in repo.list_records(KIND)
               if p["status"] == "pending" and p["proposer"] == proposer]
    if len(pending) >= MAX_PENDING_PER_PROPOSER:
        raise VcmError(f"{proposer} already has {len(pending)} pending proposals; a person "
                       "needs to approve or reject some first")
    resolved, preview = precheck(repo, action, args)
    rec = {"id": _next_id(repo), "action": action, "args": resolved,
           "rationale": rationale.strip()[:2000], "proposer": proposer, "status": "pending",
           "created_at": now_iso(), "preview": preview, "summary": ACTIONS[action]["summary"]}
    repo.save_record(KIND, rec["id"], rec)
    repo.audit.append("proposal.create", rec["id"], {"action": action, "args": resolved},
                      actor=proposer)
    return rec


def get_proposal(repo: Repository, pid: str) -> dict:
    if not _ID.match(pid):
        raise VcmError(f"invalid proposal id: {pid!r}")
    return repo.load_record(KIND, pid)


def list_proposals(repo: Repository, status: str | None = None) -> list[dict]:
    items = repo.list_records(KIND)
    return [p for p in items if status in (None, "", p["status"])]


@contextmanager
def _acting_as(name: str):
    """Underlying actions audit under $VCM_AUTHOR; make that the approving person."""
    old = os.environ.get("VCM_AUTHOR")
    os.environ["VCM_AUTHOR"] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("VCM_AUTHOR", None)
        else:
            os.environ["VCM_AUTHOR"] = old


def _decide_guard(rec: dict, by: str) -> None:
    if rec["status"] != "pending":
        raise VcmError(f"{rec['id']} is already {rec['status']}")
    if not by or by.startswith(AGENT_PREFIX):
        raise VcmError("a proposal must be decided by a person, not an agent")
    if by == rec["proposer"]:
        raise VcmError("a proposal cannot be decided by the one who made it")


def approve(repo: Repository, pid: str, by: str) -> dict:
    rec = get_proposal(repo, pid)
    _decide_guard(rec, by)
    # the repository may have moved since the proposal was made: check again, refuse if it no
    # longer holds, rather than act on a stale picture
    args, _ = precheck(repo, rec["action"], rec["args"])
    with _acting_as(by):
        if rec["action"] == "release.create":
            m = create_release(repo, args["name"], args["rev"])
            result = {"release": m["name"], "manifest_sha256": m["manifest_sha256"]}
        elif rec["action"] == "pr.open":
            pr = open_pr(repo, args["base"], args["head"], args["title"],
                         f"{rec['proposer']} (approved by {by})")
            result = {"pr": pr["id"]}
        elif rec["action"] == "fleet.place":
            place_device(repo, args["serial"], args["rack"], args.get("position"))
            result = {"serial": args["serial"], "rack": args["rack"]}
        elif rec["action"] == "rack.define":
            define_rack(repo, args["name"], args.get("power_budget_w"), None,
                        args.get("site", ""), args.get("units"))
            result = {"rack": args["name"]}
        else:
            d = register_device(repo, args["serial"], args["release"], args.get("batch", ""),
                                args.get("notes", ""))
            result = {"serial": d["serial"]}
    rec.update(status="approved", decided_by=by, decided_at=now_iso(), result=result)
    repo.save_record(KIND, pid, rec)
    repo.audit.append("proposal.approve", pid, {"action": rec["action"], "result": result,
                                                "proposer": rec["proposer"]}, actor=by)
    return rec


def reject(repo: Repository, pid: str, by: str, reason: str = "") -> dict:
    rec = get_proposal(repo, pid)
    _decide_guard(rec, by)
    rec.update(status="rejected", decided_by=by, decided_at=now_iso(), reason=reason[:1000])
    repo.save_record(KIND, pid, rec)
    repo.audit.append("proposal.reject", pid, {"action": rec["action"], "reason": reason[:200],
                                               "proposer": rec["proposer"]}, actor=by)
    return rec


def render(rec: dict) -> str:
    lines = [f"{rec['id']} [{rec['status']}] {rec['action']}  proposed by {rec['proposer']} "
             f"at {rec['created_at']}", f"  {rec['summary']}",
             "  args: " + ", ".join(f"{k}={v}" for k, v in sorted(rec["args"].items())),
             f"  why:  {rec['rationale']}"]
    if rec.get("preview"):
        lines.append("  preview: " + ", ".join(f"{k}={v}" for k, v in rec["preview"].items()
                                               if v not in (None, "")))
    if rec["status"] != "pending":
        lines.append(f"  {rec['status']} by {rec.get('decided_by')} at {rec.get('decided_at')}"
                     + (f": {rec['reason']}" if rec.get("reason") else "")
                     + (f"  result {rec['result']}" if rec.get("result") else ""))
    return "\n".join(lines)
