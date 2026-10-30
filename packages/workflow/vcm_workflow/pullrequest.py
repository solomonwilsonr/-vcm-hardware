"""Local hardware pull requests: base/head revisions with diff, impact, validation, review state."""
from __future__ import annotations

from vcm_ai import analyze
from vcm_core import Repository, VcmError, canonical_json, now_iso, sha256_bytes
from vcm_core.signing import add_signature, sign_digest, verify_signature
from vcm_diff import diff_revisions
from vcm_validation import compare_validation, validate_revision

KIND = "prs"
APPROVAL_PURPOSE = "pr-approval"


def approval_digest(pr: dict) -> str:
    """What an approval vouches for: this PR id, at exactly this base and head."""
    return sha256_bytes(canonical_json({"pr": pr["id"], "base": pr["base"], "head": pr["head"]}))


def _verified(repo: Repository, pr: dict, approval: dict) -> bool:
    sig = approval.get("signature")
    if not sig:
        return False
    res = verify_signature(sig, approval_digest(pr), APPROVAL_PURPOSE, repo.root)
    return res["valid"] and res["trusted"]


def _next_id(repo: Repository) -> int:
    return max((p["id"] for p in repo.list_records(KIND)), default=0) + 1


def open_pr(repo: Repository, base: str, head: str, title: str, author: str = "unknown") -> dict:
    b, h = repo.resolve(base), repo.resolve(head)
    for rid in (b, h):
        if not repo.latest_evidence(rid, "validation"):
            validate_revision(repo, rid)
    pr = {"id": _next_id(repo), "title": title, "state": "open", "author": author,
          "created_at": now_iso(), "base": b, "head": h, "base_ref": base, "head_ref": head,
          "diff": diff_revisions(repo, b, h), "impact": analyze(repo, b, h),
          "validation": compare_validation(repo, b, h), "comments": [], "approvals": []}
    repo.save_record(KIND, str(pr["id"]), pr)
    return get_pr(repo, pr["id"])


def get_pr(repo: Repository, pr_id: int) -> dict:
    pr = repo.load_record(KIND, str(pr_id))
    crit = [f["id"] for f in pr["impact"]["findings"] if f["severity"] == "critical"]
    regression = pr["validation"]["regression"]
    cfg = repo.config()
    need = max(1, int(cfg.get("required_approvals", 1)))
    signed_only = bool(cfg.get("require_signed_approvals"))
    for a in pr["approvals"]:  # re-checked on every read, so revoking a key withdraws its approvals
        a["verified"] = _verified(repo, pr, a)
    counted = [a for a in pr["approvals"] if a["verified"] or not signed_only]
    have = len(counted)
    blockers = []
    if have < need:
        blockers.append(f"needs {need - have} more " + ("signed " if signed_only else "") + "approval(s)")
    if regression:
        blockers.append("validation regression")
    pr["status"] = {"approvals": have, "required_approvals": need, "signed_approvals_required": signed_only, "critical_findings": crit,
                    "validation_regression": regression, "blockers": blockers,
                    "ready_for_merge": pr["state"] == "open" and not blockers}
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
        if by == pr["author"] and by != "unknown":
            raise VcmError("an author cannot approve their own pull request")
        if any(a["by"] == by for a in pr["approvals"]):
            return
        pr["approvals"].append({"by": by, "at": now_iso()})
    return _update(repo, pr_id, fn)


def approve_signed(repo: Repository, pr_id: int, key_name: str) -> dict:
    """Approve with a signature. The approver's identity is the *trusted key's name* from
    ``trustdb.json``, so it cannot be claimed by typing someone else's name."""
    pr = repo.load_record(KIND, str(pr_id))
    sig = sign_digest(key_name, approval_digest(pr), APPROVAL_PURPOSE)
    check = verify_signature(sig, approval_digest(pr), APPROVAL_PURPOSE, repo.root)
    if not check["trusted"]:
        raise VcmError(f"key {key_name} is not trusted for this project ({check['detail']})")
    who = sig["name"]

    def fn(p):
        if who == p["author"] and who != "unknown":
            raise VcmError("an author cannot approve their own pull request")
        p["approvals"] = [a for a in p["approvals"] if a["by"] != who]
        approval = {"by": who, "at": now_iso()}
        add_signature(approval, sig)
        approval["signature"] = approval.pop("signatures")[0]
        p["approvals"].append(approval)
    return _update(repo, pr_id, fn)


def _target_branch(repo: Repository, pr: dict) -> str:
    """The branch this PR merges into: the named base branch, or the one branch sitting on base."""
    branches = repo.branches()
    if pr.get("base_ref") in branches:
        return pr["base_ref"]
    at_base = [n for n, tip in branches.items() if tip == pr["base"]]
    if len(at_base) == 1:
        return at_base[0]
    raise VcmError("cannot merge: the PR base is not the tip of exactly one branch "
                   "(open the PR against a branch name)")


def merge_pr(repo: Repository, pr_id: int, by: str) -> dict:
    """Fast-forward the base branch to the PR head. There is no merge commit and no three-way
    merge: if the base branch has moved or the histories diverged the PR must be redone."""
    if not by or by == "unknown":
        raise VcmError("merging needs an identity (--by)")
    pr = get_pr(repo, pr_id)
    if pr["state"] != "open":
        raise VcmError(f"PR {pr_id} is {pr['state']}")
    if pr["status"]["blockers"]:
        raise VcmError(f"PR {pr_id} is not mergeable: " + "; ".join(pr["status"]["blockers"]))
    branch = _target_branch(repo, pr)
    if repo.branches()[branch] != pr["base"]:
        raise VcmError(f"{branch} has moved since PR {pr_id} was opened; open a new PR")
    repo.fast_forward(branch, pr["head"])
    raw = repo.load_record(KIND, str(pr_id))
    raw.update(state="merged", merged_by=by, merged_at=now_iso(), merged_into=branch)
    repo.save_record(KIND, str(pr_id), raw)
    return get_pr(repo, pr_id)


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
         f"  approvals: {', '.join(a['by'] + (' (signed)' if a.get('verified') else '') for a in pr['approvals']) or 'none'}"
         f" (need {s['required_approvals']})",
         f"  ready for merge: {s['ready_for_merge']}"
         + (f"  blocked: {'; '.join(s['blockers'])}" if s["blockers"] else "")]
    if pr["state"] == "merged":
        L.append(f"  merged into {pr['merged_into']} by {pr['merged_by']} at {pr['merged_at']}")
    for c in pr["comments"]:
        L.append(f"  comment by {c['author']}: {c['text']}")
    return "\n".join(L)
