"""Hardware Time Machine: timeline of revisions and A->B explanations."""
from __future__ import annotations

from vcm_ai import analyze, to_markdown
from vcm_core import Repository
from vcm_diff import diff_designs, diff_revisions, render_text
from vcm_parsers import Design, design_from_revision
from vcm_trace import affected_by_revision, list_devices
from vcm_validation import compare_validation


def timeline(repo: Repository, start: str = "HEAD") -> list[dict]:
    revs = repo.log(start)
    tags = repo.tags()
    releases = repo.list_records("releases")
    devices = list_devices(repo)
    out = []
    for rev in revs:
        parent = repo.get_revision(rev.parents[0]) if rev.parents else None
        d = diff_designs(design_from_revision(repo, parent) if parent else Design(),
                         design_from_revision(repo, rev))
        val = repo.latest_evidence(rev.id, "validation")
        out.append({**rev.summary(),
                    "tags": sorted(t for t, rid in tags.items() if rid == rev.id),
                    "changes": {k: v for k, v in d["summary"].items() if v},
                    "changed_components": d["changed_refs"],
                    "validation": val["status"] if val else None,
                    "releases": [r["name"] for r in releases if r["revision"] == rev.id],
                    "devices": sum(1 for dv in devices if dv["revision"] == rev.id)})
    return out


def render_timeline(items: list[dict]) -> str:
    L = []
    for it in items:
        tags = f" ({', '.join(it['tags'])})" if it["tags"] else ""
        ch = ", ".join(f"{k} {v}" for k, v in it["changes"].items()) or "no semantic change"
        extra = []
        if it["validation"]:
            extra.append(f"validation {it['validation']}")
        if it["releases"]:
            extra.append("releases " + "/".join(it["releases"]))
        if it["devices"]:
            extra.append(f"{it['devices']} device(s)")
        L.append(f"* {it['short']}{tags} {it['timestamp']} {it['author']}: {it['message']}")
        L.append(f"    {ch}" + (f" | {'; '.join(extra)}" if extra else ""))
    return "\n".join(L)


def explain(repo: Repository, rev_a: str, rev_b: str) -> dict:
    a, b = repo.resolve(rev_a), repo.resolve(rev_b)
    diff = diff_revisions(repo, a, b)
    impact = analyze(repo, a, b)
    validation = compare_validation(repo, a, b)
    devices = affected_by_revision(repo, b)
    ra, rb = repo.get_revision(a), repo.get_revision(b)
    text = "\n".join([
        f"# Explain {a[:10]} -> {b[:10]}", "",
        f"From: {ra.message} ({ra.timestamp})", f"To:   {rb.message} ({rb.timestamp})", "",
        "## What changed", "", "```", render_text(diff), "```", "",
        "## What could be affected, and the evidence", "", to_markdown(impact).split("\n", 2)[2], "",
        "## Validation", "",
        f"{validation['status']['from']} -> {validation['status']['to']}; "
        f"{len(validation['new_issues'])} new issue(s), {len(validation['resolved_issues'])} resolved.", "",
        "## Physical devices", "",
        ("Devices built from the target revision or later: "
         + (", ".join(d["serial"] for d in devices["devices"]) or "none registered")),
    ])
    return {"from": a, "to": b, "diff": diff, "impact": impact, "validation": validation,
            "devices": devices, "text": text}
