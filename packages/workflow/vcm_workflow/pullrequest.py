"""Local hardware pull requests: base/head revisions with diff, impact, validation, review state."""
from __future__ import annotations

from vcm_ai import analyze
from vcm_core import Repository, VcmError, now_iso
from vcm_diff import diff_revisions
from vcm_validation import compare_validation, validate_revision

KIND = "prs"


def _next_id(repo: Repository) -> int:
    return max((p["id"] for p in repo.list_records(KIND)), default=0) + 1


def open_pr(repo: Repository, base: str, head: str, title: str, author: str = "unknown") -> dict:
    b, h = repo.resolve(base), repo.resolve(head)
    for rid in (b, h):
        if not repo.latest_evidence(rid, "validation"):
            validate_revision(repo, rid)
    pr = {"id": _next_id(repo), "title": title, "state": "open", "author": author,
          "created_at": now_iso(), "base": b, "head": h,
          "diff": diff_revisions(repo, b, h), "impact": analyze(repo, b, h),
          "validation": compare_validation(repo, b, h), "comments": [], "approvals": []}
    repo.save_record(KIND, str(pr["id"]), pr)
    return get_pr(repo, pr["id"])


def get_pr(repo: Repository, pr_id: int) -> dict:
    pr = repo.load_record(KIND, str(pr_id))
    crit = [f["id"] for f in pr["impact"]["findings"] if f["severity"] == "critical"]
    regression = pr["validation"]["regression"]
    pr["status"] = {"approvals": len(pr["approvals"]), "critical_findings": crit,
                    "validation_regression": regression,
                    "ready_for_merge": pr["state"] == "open" and bool(pr["approvals"]) and not regression}
    return pr


def list_prs(repo: Repository) -> list[dict]:
    return [{"id": p["id"], "title": p["title"], "state": p["state"], "base": p["base"][:10],
             "head": p["head"][:10], "approvals": len(p["approvals"])}
            for p in repo.list_records(KIND)]


def _update(repo: Repository, pr_id: int, fn) -> dict:
    pr = repo.load_record(KIND, str(pr_id))
    if pr["state"] != "open":
        raise VcmError(f"PR {pr_id} is {pr['state']}")
    fn(pr)
    repo.save_record(KIND, str(pr_id), pr)
    return get_pr(repo, pr_id)


def add_comment(repo: Repository, pr_id: int, author: str, text: str) -> dict:
    return _update(repo, pr_id, lambda pr: pr["comments"].append(
        {"author": author, "text": text, "at": now_iso()}))


def approve(repo: Repository, pr_id: int, by: str) -> dict:
    def fn(pr):
        if any(a["by"] == by for a in pr["approvals"]):
            return
        pr["approvals"].append({"by": by, "at": now_iso()})
    return _update(repo, pr_id, fn)


def close_pr(repo: Repository, pr_id: int) -> dict:
    return _update(repo, pr_id, lambda pr: pr.update(state="closed"))


def render_pr(pr: dict) -> str:
    s, d = pr["status"], pr["diff"]["summary"]
    L = [f"PR #{pr['id']} [{pr['state']}] {pr['title']}", f"  base {pr['base'][:10]} <- head {pr['head'][:10]}"
         f"  by {pr['author']}",
         "  diff: " + ", ".join(f"{k} {v}" for k, v in d.items() if v),
         "  impact: " + ", ".join(f"{k} {v}" for k, v in pr["impact"]["summary"]["by_class"].items()),
         f"  critical findings: {', '.join(s['critical_findings']) or 'none'}",
         f"  validation: {pr['validation']['status']['from']} -> {pr['validation']['status']['to']}"
         f"{' (REGRESSION)' if s['validation_regression'] else ''}",
         f"  approvals: {', '.join(a['by'] for a in pr['approvals']) or 'none'}",
         f"  ready for merge: {s['ready_for_merge']}"]
    for c in pr["comments"]:
        L.append(f"  comment by {c['author']}: {c['text']}")
    return "\n".join(L)
