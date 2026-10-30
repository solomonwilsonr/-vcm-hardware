"""Policies as code: the rules a team agrees a change must meet, committed beside the design.

``vcm-policy.json`` at the repository root::

    {"schema": "vcm-policy/1",
     "rules": [
       {"id": "no-critical", "forbid_severity": "critical"},
       {"id": "coverage", "min_catalog_coverage": 0.9},
       {"id": "known-parts", "forbid_unknown_parts": true},
       {"id": "power-needs-two", "require_approvals": 2,
        "when": {"title_matches": "power|supply|cooling"}},
       {"id": "no-regressions", "forbid_validation_regression": true},
       {"id": "checked-against-machine", "require_observed_base": true},
       {"id": "signed", "require_signed_release": true, "stages": ["verify"]}
     ]}

Each rule may limit itself to ``stages`` (``check``, ``review``, ``release``, ``verify``) and to
changes whose findings match ``when`` (``title_matches`` regex, ``severity_at_least``). A rule
that cannot be evaluated at a stage (approvals before there is a review) is reported as skipped,
not as passed.

The policy used is the one committed in the revision being evaluated, so the rules that applied
to a change are recoverable from history.
"""
from __future__ import annotations

import json
import re

from vcm_core import Repository, VcmError

POLICY_FILE = "vcm-policy.json"
STAGES = ("check", "review", "release", "verify")
_SEV = {"info": 0, "warning": 1, "critical": 2}
_NEEDS_REPORT = {"forbid_severity", "min_catalog_coverage", "forbid_unknown_parts",
                 "forbid_validation_regression"}
KNOWN = {"forbid_severity", "min_catalog_coverage", "forbid_unknown_parts", "require_approvals",
         "forbid_validation_regression", "require_observed_base", "require_signed_release"}


def load_policy(repo: Repository, rev: str | None = None) -> dict | None:
    """The policy committed at ``rev`` (default: the working tree), or None when there is none."""
    if rev is not None:
        r = repo.get_revision(rev)
        if POLICY_FILE not in r.artifacts:
            return None
        text = repo.read_artifact(r, POLICY_FILE).decode()
    else:
        p = repo.root / POLICY_FILE
        if not p.is_file():
            return None
        text = p.read_text()
    try:
        pol = json.loads(text)
    except ValueError as exc:
        raise VcmError(f"{POLICY_FILE} is not valid JSON: {exc}") from exc
    for rule in pol.get("rules", []):
        if not rule.get("id"):
            raise VcmError(f"{POLICY_FILE}: every rule needs an id")
        if not KNOWN & set(rule):
            raise VcmError(f"{POLICY_FILE}: rule {rule['id']} has no known check "
                           f"({', '.join(sorted(KNOWN))})")
    return pol


def _matching(report: dict, when: dict) -> list[dict]:
    pat = re.compile(when["title_matches"], re.I) if when.get("title_matches") else None
    floor = _SEV.get(when.get("severity_at_least", "info"), 0)
    return [f for f in report["findings"]
            if _SEV.get(f["severity"], 0) >= floor and (pat is None or pat.search(f["title"]))]


def evaluate(policy: dict | None, report: dict | None, stage: str, approvals: int | None = None,
             signed: bool | None = None, observed_base: bool | None = None) -> dict:
    """Evaluate every rule; returns passed, violated and skipped rules with reasons."""
    if stage not in STAGES:
        raise VcmError(f"unknown stage {stage!r}")
    out = {"stage": stage, "policy": bool(policy), "ok": True, "violations": [],
           "passed": [], "skipped": []}
    for rule in (policy or {}).get("rules", []):
        rid = rule["id"]
        if rule.get("stages") and stage not in rule["stages"]:
            continue
        if rule.get("when") and report is not None and not _matching(report, rule["when"]):
            out["passed"].append({"id": rid, "detail": "does not apply: no matching findings"})
            continue
        problems, skipped = [], None
        if report is None and _NEEDS_REPORT & set(rule):
            out["skipped"].append({"id": rid, "detail": rule.get("message", ""),
                                   "reason": "no change to evaluate (first revision)"})
            continue
        if "forbid_severity" in rule and report is not None:
            floor = _SEV[rule["forbid_severity"]]
            bad = [f"{f['id']} {f['title']}" for f in report["findings"]
                   if _SEV.get(f["severity"], 0) >= floor]
            if bad:
                problems.append(f"{len(bad)} finding(s) at {rule['forbid_severity']} or above: "
                                + "; ".join(bad[:3]))
        if "min_catalog_coverage" in rule and report is not None:
            cov = report["summary"]["catalog_coverage"]
            if cov < float(rule["min_catalog_coverage"]):
                problems.append(f"catalog covers {cov:.0%} of changed components, policy "
                                f"requires {float(rule['min_catalog_coverage']):.0%}")
        if rule.get("forbid_unknown_parts") and report is not None:
            unknown = report.get("catalog", {}).get("unknown", [])
            if unknown:
                problems.append(f"parts unknown to the catalog: {', '.join(unknown[:6])}")
        if rule.get("forbid_validation_regression") and report is not None:
            if any(f["title"].startswith("Validation ") and f["severity"] == "critical"
                   for f in report["findings"]):
                problems.append("validation regressed")
        if "require_approvals" in rule:
            if approvals is None:
                skipped = "approvals only exist on a review"
            elif approvals < int(rule["require_approvals"]):
                problems.append(f"{approvals} approval(s), policy requires "
                                f"{int(rule['require_approvals'])}")
        if rule.get("require_observed_base"):
            if observed_base is None:
                skipped = "no base revision to have observed"
            elif not observed_base:
                problems.append("the base configuration has not been checked against the "
                                "machine (vcm drift --attach)")
        if rule.get("require_signed_release"):
            if signed is None:
                skipped = "only a release can be signed"
            elif not signed:
                problems.append("release is not signed by a trusted key")
        entry = {"id": rid, "detail": rule.get("message", "")}
        if problems:
            out["violations"].append({**entry, "problems": problems})
        elif skipped:
            out["skipped"].append({**entry, "reason": skipped})
        else:
            out["passed"].append(entry)
    out["ok"] = not out["violations"]
    return out


def observed(repo: Repository, rev: str | None) -> bool | None:
    if rev is None:
        return None
    return repo.latest_evidence(rev, "observed") is not None


def render(res: dict) -> str:
    if not res["policy"]:
        return f"no {POLICY_FILE}: nothing to enforce"
    lines = [f"policy ({res['stage']}): {'PASS' if res['ok'] else 'FAIL'}"]
    for v in res["violations"]:
        for p in v["problems"]:
            lines.append(f"  VIOLATION {v['id']}: {p}")
    for s in res["skipped"]:
        lines.append(f"  skipped   {s['id']}: {s['reason']}")
    for p in res["passed"]:
        lines.append(f"  pass      {p['id']}" + (f" ({p['detail']})" if p["detail"] else ""))
    return "\n".join(lines)
