"""Semantic hardware diff: BOM, PCB footprints/placement, and net connectivity."""
from __future__ import annotations

import math
from collections import Counter

from vcm_parsers import Component, Design, design_from_revision

MOVE_EPS_MM = 0.01
_IDENTITY = ("mpn", "lib_id")
_TRACKED = ("value", "footprint", "mpn", "lib_id", "manufacturer", "voltage")


def _brief(c: Component) -> dict:
    return {"value": c.value, "footprint": c.footprint, "mpn": c.mpn,
            "lib_id": c.lib_id, "manufacturer": c.manufacturer, "voltage": c.voltage}


def _placement(c: Component) -> dict:
    return {"x": c.x, "y": c.y, "rotation": c.rotation, "layer": c.layer}


def diff_designs(old: Design, new: Design) -> dict:
    o, n = old.components, new.components
    added = [{"ref": r, **_brief(n[r])} for r in sorted(set(n) - set(o))]
    removed = [{"ref": r, **_brief(o[r])} for r in sorted(set(o) - set(n))]
    replaced, value_changed, fp_changes, moved = [], [], [], []

    for ref in sorted(set(o) & set(n)):
        a, b = o[ref], n[ref]
        changed = [f for f in _TRACKED if getattr(a, f) != getattr(b, f)]
        if any(f in changed for f in _IDENTITY):
            replaced.append({"ref": ref, "old": _brief(a), "new": _brief(b), "changed_fields": changed})
        elif "value" in changed:
            value_changed.append({"ref": ref, "old_value": a.value, "new_value": b.value})
        if "footprint" in changed and (a.footprint or b.footprint):
            fp_changes.append({"ref": ref, "old": a.footprint, "new": b.footprint})
        if a.x is not None and b.x is not None:
            dist = math.hypot((b.x or 0) - (a.x or 0), (b.y or 0) - (a.y or 0))
            if dist > MOVE_EPS_MM or a.rotation != b.rotation or a.layer != b.layer:
                moved.append({"ref": ref, "old": _placement(a), "new": _placement(b),
                              "distance_mm": round(dist, 4)})

    def counts(d: Design) -> Counter:
        return Counter((c.value, c.footprint) for c in d.components.values())

    co, cn = counts(old), counts(new)
    count_changes = [{"value": k[0], "footprint": k[1], "old": co.get(k, 0), "new": cn.get(k, 0)}
                     for k in sorted(set(co) | set(cn)) if co.get(k, 0) != cn.get(k, 0)]

    no, nn = old.nets, new.nets
    net_changes = []
    for net in sorted(set(no) & set(nn)):
        add, rem = sorted(set(nn[net]) - set(no[net])), sorted(set(no[net]) - set(nn[net]))
        if add or rem:
            net_changes.append({"net": net, "pins_added": add, "pins_removed": rem})

    nets_added, nets_removed = sorted(set(nn) - set(no)), sorted(set(no) - set(nn))
    touched = {x["ref"] for x in added + removed + replaced + value_changed + fp_changes + moved}
    for ch in net_changes:
        touched |= {p.split(".")[0] for p in ch["pins_added"] + ch["pins_removed"]}
    for net in nets_added:
        touched |= {p.split(".")[0] for p in nn[net]}
    for net in nets_removed:
        touched |= {p.split(".")[0] for p in no[net]}

    return {
        "summary": {"added": len(added), "removed": len(removed), "replaced": len(replaced),
                    "value_changed": len(value_changed), "footprint_changed": len(fp_changes),
                    "moved": len(moved), "count_changes": len(count_changes),
                    "nets_added": len(nets_added), "nets_removed": len(nets_removed),
                    "nets_changed": len(net_changes)},
        "bom": {"added": added, "removed": removed, "replaced": replaced,
                "value_changed": value_changed, "count_changes": count_changes},
        "pcb": {"footprint_changes": fp_changes, "moved": moved},
        "nets": {"added": nets_added, "removed": nets_removed, "changed": net_changes},
        "changed_refs": sorted(touched),
    }


def diff_revisions(repo, a: str, b: str) -> dict:
    """Artifact-level plus semantic design diff between two revisions."""
    ra, rb = repo.get_revision(a), repo.get_revision(b)
    result = diff_designs(design_from_revision(repo, ra), design_from_revision(repo, rb))
    return {"from": ra.id, "to": rb.id, "artifacts": repo.diff_artifacts(ra.id, rb.id), **result}


def render_text(d: dict) -> str:
    """Human-readable rendering of a diff result."""
    L: list[str] = []
    if "from" in d:
        L.append(f"Diff {d['from'][:10]} -> {d['to'][:10]}")
        art = d["artifacts"]
        for kind in ("added", "removed", "modified"):
            for x in art[kind]:
                L.append(f"  [{x['category']}] {kind}: {x['path']}")
    s = d["summary"]
    if not any(s.values()):
        L.append("No semantic hardware changes.")
        return "\n".join(L)
    L.append("BOM:")
    for x in d["bom"]["added"]:
        L.append(f"  + {x['ref']}  {x['value']}  ({x['footprint']})")
    for x in d["bom"]["removed"]:
        L.append(f"  - {x['ref']}  {x['value']}  ({x['footprint']})")
    for x in d["bom"]["replaced"]:
        a, b = x["old"], x["new"]
        L.append(f"  ~ {x['ref']} replaced: {a['value']} [{a['mpn'] or a['lib_id']}] -> "
                 f"{b['value']} [{b['mpn'] or b['lib_id']}]  (changed: {', '.join(x['changed_fields'])})")
    for x in d["bom"]["value_changed"]:
        L.append(f"  ~ {x['ref']} value: {x['old_value']} -> {x['new_value']}")
    for x in d["bom"]["count_changes"]:
        L.append(f"  # count {x['value']} ({x['footprint']}): {x['old']} -> {x['new']}")
    if d["pcb"]["footprint_changes"] or d["pcb"]["moved"]:
        L.append("PCB:")
    for x in d["pcb"]["footprint_changes"]:
        L.append(f"  footprint {x['ref']}: {x['old']} -> {x['new']}")
    for x in d["pcb"]["moved"]:
        o, n = x["old"], x["new"]
        L.append(f"  moved {x['ref']}: ({o['x']}, {o['y']}) -> ({n['x']}, {n['y']}) "
                 f"[{x['distance_mm']} mm]")
    nets = d["nets"]
    if nets["added"] or nets["removed"] or nets["changed"]:
        L.append("Nets:")
    for name in nets["added"]:
        L.append(f"  + net {name}")
    for name in nets["removed"]:
        L.append(f"  - net {name}")
    for x in nets["changed"]:
        L.append(f"  net {x['net']}: +{', '.join(x['pins_added']) or '-'}  -{', '.join(x['pins_removed']) or '-'}")
    L.append("Changed components: " + ", ".join(d["changed_refs"]))
    return "\n".join(L)
