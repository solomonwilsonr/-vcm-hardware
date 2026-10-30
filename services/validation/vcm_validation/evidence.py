"""Validation runs stored as versioned evidence on a revision, and cross-revision comparison."""
from __future__ import annotations

from vcm_parsers import design_from_revision

from .checks import run_checks
from .kicad_cli import run_kicad_checks

KIND = "validation"


def validate_revision(repo, rev: str = "HEAD", run_kicad: bool = False, attach: bool = True) -> dict:
    revision = repo.get_revision(rev)
    design = design_from_revision(repo, revision)
    issues = run_checks(design)
    counts = {"error": sum(i["severity"] == "error" for i in issues),
              "warning": sum(i["severity"] == "warning" for i in issues)}
    result = {
        "revision": revision.id, "tool": "vcm-builtin",
        "status": "fail" if counts["error"] else "pass",
        "counts": counts, "issues": issues,
        "components_checked": len(design.components),
        "kicad_cli": None,
    }
    if run_kicad:
        files = {p: repo.store.read(a.hash) for p, a in revision.artifacts.items()
                 if p.lower().endswith((".kicad_pcb", ".kicad_sch"))}
        result["kicad_cli"] = run_kicad_checks(files)
        if result["kicad_cli"]["status"] == "fail":
            result["status"] = "fail"
    if attach:
        entry = repo.attach_evidence(revision.id, KIND, result)
        result["_evidence"] = entry
    return result


def _key(i: dict) -> tuple:
    return (i["code"], i["ref"], i["message"])


def compare_validation(repo, a: str, b: str) -> dict:
    """Compare stored (or freshly computed, unattached) validation results of two revisions."""
    def get(rev: str) -> dict:
        return repo.latest_evidence(rev, KIND) or validate_revision(repo, rev, attach=False)
    ra, rb = get(a), get(b)
    ka, kb = {_key(i): i for i in ra["issues"]}, {_key(i): i for i in rb["issues"]}
    return {"from": repo.resolve(a), "to": repo.resolve(b),
            "status": {"from": ra["status"], "to": rb["status"]},
            "counts": {"from": ra["counts"], "to": rb["counts"]},
            "new_issues": [kb[k] for k in sorted(set(kb) - set(ka))],
            "resolved_issues": [ka[k] for k in sorted(set(ka) - set(kb))],
            "regression": rb["counts"]["error"] > ra["counts"]["error"]}
