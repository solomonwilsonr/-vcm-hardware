"""Render an impact report as Markdown with the three confidence classes clearly separated."""
from __future__ import annotations

SECTIONS = (
    ("verified", "Verified", "Derived from parsed design data, declared project links or stored evidence."),
    ("rule_based", "Rule-based", "Heuristic warnings from a small component DB and engineering rules. Check against datasheets."),
    ("speculative", "Speculative", "Predictions. Not established by data; treat as questions to investigate."),
)


def _ev(e: dict) -> str:
    t = e["type"]
    if t == "file":
        return f"file `{e['path']}` @ {e['hash'][:10]}"
    if t == "commit":
        return f"commit `{e['revision'][:10]}` ({e.get('role', '')})"
    if t == "ref":
        return f"ref `{e['ref']}`"
    return f"{t} `{e.get('part', '')}`"


def to_markdown(report: dict) -> str:
    s = report["summary"]
    L = [f"# Impact report {report['from'][:10]} -> {report['to'][:10]}", "",
         f"Changed components: {', '.join(s['changed_components']) or 'none'}  ",
         "Findings: " + ", ".join(f"{k} {v}" for k, v in s["by_class"].items())
         + " | severity: " + ", ".join(f"{k} {v}" for k, v in s["by_severity"].items()), ""]
    for cls, title, blurb in SECTIONS:
        items = [f for f in report["findings"] if f["confidence_class"] == cls]
        L += [f"## {title} ({len(items)})", f"_{blurb}_", ""]
        for f in items:
            L.append(f"- **[{f['severity']}] {f['id']} {f['title']}**")
            if f["detail"]:
                L.append(f"  - {f['detail']}")
            if f.get("derived_from"):
                L.append(f"  - derived from: {', '.join(f['derived_from'])}")
            L.append("  - evidence: " + "; ".join(_ev(e) for e in f["evidence"]))
        if not items:
            L.append("- none")
        L.append("")
    L += ["## Required re-tests", ""]
    for r in report["required_retests"]:
        L.append(f"- **{r['name']}** ({r['confidence_class']}): {r['reason']}")
    if not report["required_retests"]:
        L.append("- none")
    return "\n".join(L) + "\n"
