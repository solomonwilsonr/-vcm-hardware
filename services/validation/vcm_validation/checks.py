"""Built-in design and BOM checks (no external tools)."""
from __future__ import annotations

from vcm_parsers import Design
from vcm_parsers.bom import parse_bom


def issue(code: str, severity: str, message: str, ref: str = "", file: str = "") -> dict:
    return {"code": code, "severity": severity, "message": message, "ref": ref, "file": file}


def check_components(design: Design) -> list[dict]:
    out: list[dict] = []
    for d in design.duplicates:
        out.append(issue("DUPLICATE_REF", "error", f"duplicate reference {d['ref']}", d["ref"], d["source"]))
    for ref, c in sorted(design.components.items()):
        if not c.value:
            out.append(issue("MISSING_VALUE", "error", f"{ref} has no value", ref))
        if not c.footprint:
            out.append(issue("MISSING_FOOTPRINT", "error", f"{ref} has no footprint", ref))
        for fld in ("value", "footprint"):
            vals = {k: v[fld] for k, v in c.by_source.items() if v[fld]}
            if len(set(vals.values())) > 1:
                detail = ", ".join(f"{k}={v}" for k, v in sorted(vals.items()))
                out.append(issue("SOURCE_MISMATCH", "warning", f"{ref} {fld} differs between sources: {detail}", ref))
        if len(design.sources) > 1 and len(c.by_source) < len(design.sources):
            missing = sorted(set(design.sources) - set(c.by_source))
            out.append(issue("MISSING_IN_SOURCE", "warning", f"{ref} absent from {', '.join(missing)}", ref))
    return out


def check_nets(design: Design) -> list[dict]:
    out: list[dict] = []
    for net, pins in design.nets.items():
        if len(pins) == 1:
            out.append(issue("UNCONNECTED_NET", "warning",
                             f"net {net} has a single connection ({pins[0]})", pins[0].split(".")[0]))
    for net in sorted(design.declared_nets - set(design.nets)):
        out.append(issue("EMPTY_NET", "warning", f"net {net} is declared but has no pads"))
    for pin in design.unconnected_pads():
        out.append(issue("UNCONNECTED_PAD", "warning", f"pad {pin} has no net", pin.split(".")[0]))
    return out


def check_bom_text(text: str, path: str = "") -> list[dict]:
    """Validate a BOM CSV by itself: duplicates, missing fields, quantity mismatches."""
    comps, dups, mism = parse_bom(text)
    out = [issue("DUPLICATE_REF", "error", f"duplicate reference {d}", d, path) for d in dups]
    for c in comps:
        if not c.value:
            out.append(issue("MISSING_VALUE", "error", f"{c.ref} has no value", c.ref, path))
        if not c.footprint:
            out.append(issue("MISSING_FOOTPRINT", "error", f"{c.ref} has no footprint", c.ref, path))
    for m in mism:
        out.append(issue("QTY_MISMATCH", "warning",
                         f"quantity {m['qty']} does not match designators {', '.join(m['refs'])}",
                         m["refs"][0] if m["refs"] else "", path))
    return out


def run_checks(design: Design) -> list[dict]:
    return check_components(design) + check_nets(design)
