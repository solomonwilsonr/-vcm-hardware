"""Drift: the declared node manifest against what the machine actually reports.

Every finding here compares two pieces of data -- a committed manifest and an observation -- so
they are facts about disagreement, not heuristics. What they cannot say is which side is right:
the manifest may be stale, or the machine may have been changed without the change being recorded.

Two rules keep this honest:

* Only types the observation **covers** are compared. A Redfish capture with no NICs in it says
  nothing about NICs, and reporting them as missing would be invented drift.
* Model names are matched by tokens, not by string equality. ``Intel(R) Xeon(R) Gold 6338 CPU @
  2.00GHz`` and ``Intel Xeon Gold 6338`` are the same part described by two different tools.
"""
from __future__ import annotations

import re

_NOISE = re.compile(r"\((r|tm|c)\)|@.*$|\bcpu\b|\bprocessor\b", re.I)
#: firmware keys the manifest declares and an observation can report under the same name
FIRMWARE_KEYS = ("bios", "bmc", "gpu_driver", "cuda")


def _tokens(text: str) -> set[str]:
    text = _NOISE.sub(" ", str(text or "").lower())
    return {t for t in re.split(r"[^a-z0-9.]+", text) if t}


def models_match(declared: dict, observed: dict) -> bool:
    """Same part, as far as the two descriptions allow us to tell."""
    dm, om = (declared.get("mpn") or "").upper(), (observed.get("mpn") or "").upper()
    if dm and om and (dm.startswith(om) or om.startswith(dm)):
        return True
    a, b = _tokens(declared.get("model", "")), _tokens(observed.get("model", ""))
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    # every distinguishing token of the shorter description appears in the longer one
    return small <= big


def _same_slot(a: str, b: str) -> bool:
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
    return bool(a) and norm(a) == norm(b)


def _declared(design) -> list[dict]:
    out = []
    for ref, c in sorted(design.components.items()):
        out.append({"ref": ref, "type": (c.lib_id or "").lower(), "model": c.value,
                    "mpn": c.mpn, "slot": (c.props or {}).get("slot", "")})
    return out


def _version_tuple(v: str) -> tuple[int, ...] | None:
    v = str(v or "").strip()
    return tuple(int(x) for x in v.split(".")) if re.fullmatch(r"\d+(\.\d+)*", v) else None


def compare(design, node_meta: dict, observed: dict) -> dict:
    """Compare a parsed node design (and its manifest-level meta) against an observation."""
    covers = set(observed.get("covers", []))
    declared = _declared(design)
    findings: list[dict] = []

    def add(severity: str, kind: str, title: str, detail: str, **extra) -> None:
        findings.append({"confidence_class": "verified", "severity": severity, "kind": kind,
                         "title": title, "detail": detail, **extra})

    types = sorted({d["type"] for d in declared} | {o["type"] for o in observed["components"]})
    checked = [t for t in types if t in covers]
    for t in checked:
        want = [d for d in declared if d["type"] == t]
        have = list(o for o in observed["components"] if o["type"] == t)
        unmatched_want = []
        # same slot and same part first, so a gap is attributed to the slot it is actually in
        for d in list(want):
            hit = next((i for i, o in enumerate(have)
                        if d["slot"] and _same_slot(d["slot"], o.get("slot", ""))
                        and models_match(d, o)), None)
            if hit is not None:
                have.pop(hit)
                want.remove(d)
        for d in want:
            hit = next((i for i, o in enumerate(have) if models_match(d, o)), None)
            if hit is None:
                unmatched_want.append(d)
            else:
                have.pop(hit)
        # pair leftovers: same count on both sides reads as "a different part is fitted"
        while unmatched_want and have:
            d, o = unmatched_want.pop(0), have.pop(0)
            add("warning", "model_mismatch", f"{d['ref']}: declared {d['model']}, machine reports "
                f"{o['model'] or o['mpn']}",
                f"The manifest declares {d['model']} ({d['mpn'] or 'no part number'}); the "
                f"{t} the machine reports is {o['model']} ({o['mpn'] or 'no part number'}"
                f"{', serial ' + o['serial'] if o.get('serial') else ''}).",
                type=t, refs=[d["ref"]], declared=d, observed=o)
        for d in unmatched_want:
            add("warning", "missing", f"{d['ref']} ({d['model']}) not reported by the machine",
                f"The manifest declares a {t} the observation does not contain. It may have been "
                "removed or failed, or the source may not enumerate it.",
                type=t, refs=[d["ref"]], declared=d)
        for o in have:
            add("warning", "unexpected", f"Machine reports an undeclared {t}: {o['model'] or o['mpn']}",
                f"Slot {o.get('slot') or 'unknown'}"
                f"{', serial ' + o['serial'] if o.get('serial') else ''}. Not in the manifest.",
                type=t, refs=[], observed=o)

    declared_fw = (node_meta or {}).get("firmware") or {}
    observed_fw = observed.get("firmware") or {}
    fw_checked = []
    for key in FIRMWARE_KEYS:
        if key not in declared_fw or key not in observed_fw:
            continue
        fw_checked.append(key)
        want, have = str(declared_fw[key]), str(observed_fw[key])
        if want == have:
            continue
        wt, ht = _version_tuple(want), _version_tuple(have)
        older = wt is not None and ht is not None and ht < wt
        add("critical" if older and key == "gpu_driver" else "warning", "firmware",
            f"{key}: declared {want}, machine runs {have}",
            ("The machine runs an OLDER version than the manifest declares. Rules that "
             "compared against the declared version reached their conclusion on the wrong "
             "input." if older else "The machine and the manifest disagree."),
            type="firmware", refs=[], declared={key: want}, observed={key: have})

    return {"schema": "vcm-drift/1", "node": observed.get("node") or (node_meta or {}).get("node"),
            "sources": observed.get("sources", []),
            "status": "drifted" if findings else "in_sync",
            "checked_types": checked,
            "unchecked_types": [t for t in types if t not in covers],
            "checked_firmware": fw_checked,
            "counts": {s: sum(f["severity"] == s for f in findings)
                       for s in ("critical", "warning", "info")},
            "findings": findings}
