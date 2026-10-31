"""Check what is about to be said against what the report actually contains.

An agent reading an impact report can misquote it: cite a finding that does not exist, promote a
``rule_based`` finding to ``verified``, round 4190 W into 4000 W, or leave out the one critical
finding that mattered. This catches those, mechanically, before the answer goes out. It cannot
catch bad *reasoning* about correct facts; nothing mechanical can.

Claims are small objects::

    {"finding": "R001", "confidence_class": "rule_based", "severity": "critical",
     "quotes": ["4190", "4000 W"]}                  # a finding, and what you will say about it
    {"type": "none", "severity": "critical", "title_contains": "cooling"}
                                                    # "there is no critical cooling finding"
    {"type": "count", "critical": 4, "speculative": 4}
    {"type": "trust", "catalog_values": 2, "catalog_verified": 0}

The report is recomputed by the caller from the repository, never taken from the agent.
"""
from __future__ import annotations

import json
import re

SEVERITIES = ("critical", "warning", "info")
CLASSES = ("verified", "rule_based", "speculative")
MAX_CLAIMS = 200


def _norm(text: str) -> str:
    """Lower-case, with thousands separators removed so '4,190 W' matches '4190 W'."""
    return re.sub(r"(?<=\d),(?=\d{3}\b)", "", str(text)).lower()


def _evidence_values(evidence: list[dict]) -> str:
    out = []
    for e in evidence:
        for v in (e.get("fields") or {}).values():
            out.append(str(v.get("value", "")))
        for k, v in e.items():
            if k not in ("fields", "type") and isinstance(v, (str, int, float)):
                out.append(str(v))
    return " ".join(out)


def haystack(finding: dict) -> str:
    return _norm(" ".join([finding["title"], finding["detail"], _evidence_values(
        finding.get("evidence", []))]))


def has_quote(text: str, quote) -> bool:
    """Whether ``quote`` appears in ``text``; numbers must match whole (190 is not 4190)."""
    q, text = _norm(quote).strip(), _norm(text)
    if not q:
        return False
    if re.fullmatch(r"-?\d+(?:\.\d+)?", q):
        if "." in q and float(q) == int(float(q)):
            q = str(int(float(q)))
        return re.search(rf"(?<![\d.]){re.escape(q)}(?:\.0+)?(?!\.?\d)", text) is not None
    return q in text


def report_text(report: dict) -> str:
    """Everything in a report an answer may legitimately quote a number from."""
    parts = [json.dumps(report["summary"], sort_keys=True)]
    parts += [haystack(f) for f in report["findings"]]
    parts += [r["reason"] for r in report.get("required_retests", [])]
    return _norm(" ".join(parts))


def check_claims(report: dict, claims: list[dict]) -> dict:
    if not isinstance(claims, list) or len(claims) > MAX_CLAIMS:
        raise ValueError(f"claims must be a list of at most {MAX_CLAIMS} objects")
    by_id = {f["id"]: f for f in report["findings"]}
    results, mentioned = [], set()
    for i, claim in enumerate(claims):
        problems: list[str] = []
        kind = claim.get("type", "finding") if isinstance(claim, dict) else "?"
        if not isinstance(claim, dict):
            problems.append("a claim must be an object")
        elif kind == "finding":
            f = by_id.get(claim.get("finding"))
            if f is None:
                problems.append(f"there is no finding {claim.get('finding')!r} in this report")
            else:
                mentioned.add(f["id"])
                for key in ("confidence_class", "severity"):
                    if key in claim and claim[key] != f[key]:
                        problems.append(f"{f['id']} is {f[key]}, not {claim[key]}")
                if claim.get("title_contains") and _norm(claim["title_contains"]) not in _norm(
                        f["title"]):
                    problems.append(f"{f['id']} is titled {f['title']!r}")
                text = haystack(f)
                for q in claim.get("quotes", []):
                    if not has_quote(text, q):
                        problems.append(f"{q!r} does not appear in {f['id']} or its evidence")
        elif kind == "none":
            if not any(claim.get(k) for k in ("severity", "confidence_class", "title_contains")):
                problems.append("a 'none' claim needs severity, confidence_class or "
                                "title_contains")
            for f in report["findings"]:
                if (claim.get("severity") in (None, f["severity"])
                        and claim.get("confidence_class") in (None, f["confidence_class"])
                        and _norm(claim.get("title_contains", "")) in _norm(f["title"])):
                    problems.append(f"but {f['id']} matches: {f['title']}")
        elif kind == "count":
            s = report["summary"]
            have = {**s["by_severity"], **s["by_class"]}
            for k, v in claim.items():
                if k != "type" and have.get(k) != v:
                    problems.append(f"the report has {have.get(k)} {k}, not {v}")
            if not set(claim) - {"type"}:
                problems.append("a 'count' claim needs at least one count")
        elif kind == "trust":
            t = report["summary"].get("trust") or {}
            for k, v in claim.items():
                if k != "type" and t.get(k) != v:
                    problems.append(f"trust.{k} is {t.get(k)}, not {v}")
        else:
            problems.append(f"unknown claim type {kind!r}")
        results.append({"claim": i, "ok": not problems, "problems": problems})
    unmentioned = [f"{f['id']} {f['title']}" for f in report["findings"]
                   if f["severity"] == "critical" and f["id"] not in mentioned]
    ok = all(r["ok"] for r in results)
    return {"ok": ok, "complete": not unmentioned, "honest": ok and not unmentioned,
            "results": results, "unmentioned_critical": unmentioned,
            "note": "complete means every critical finding in the report has a claim; an answer "
                    "that leaves one out is incomplete however accurate the rest."}
