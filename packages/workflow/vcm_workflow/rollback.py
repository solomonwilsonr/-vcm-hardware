"""Hardware rollback planner: what is software-reversible vs needs physical rework."""
from __future__ import annotations

from vcm_core import Repository
from vcm_diff import diff_revisions
from vcm_parsers import design_from_revision
from vcm_trace import list_devices

SOFTWARE_CATEGORIES = {"firmware", "fpga"}

# The mechanics of a rollback differ by what the design is. Swapping a part on a board means
# desoldering and, if the land pattern moved, new copper; swapping a part in a chassis means a
# site visit and a slot. The plan is the same, the words an engineer needs are not.
_WORDS = {
    "board": {"swap": "Desolder {ref} ({old}) and fit {new} ({mpn}), same footprint {fp}.",
              "fitting": "footprint", "carrier": "fielded boards",
              "respin": "Board respin required (not field-reworkable)",
              "respin_verb": "needs new copper",
              "refit": "Fit {ref} ({value}, {fp}) if pads exist on the board; otherwise a respin "
                       "is required."},
    "node": {"swap": "Remove {ref} ({old}) and refit {new} ({mpn}) in {fp}.",
             "fitting": "slot", "carrier": "deployed units",
             "respin": "Chassis or platform change required (not a field swap)",
             "respin_verb": "does not fit the existing slot",
             "refit": "Refit {ref} ({value}) in {fp} if the slot is free; otherwise a chassis "
                      "change is required."},
}


def _vocabulary(repo: Repository, rev: str) -> dict:
    """Pick wording from what the design actually is."""
    try:
        from vcm_ai.rules_datacenter import is_node_design
        return _WORDS["node"] if is_node_design(design_from_revision(repo, rev)) else _WORDS["board"]
    except Exception:          # vcm_ai is a higher layer; never let its absence break a rollback
        return _WORDS["board"]


def plan_rollback(repo: Repository, target: str, current: str = "HEAD") -> dict:
    cur, tgt = repo.resolve(current), repo.resolve(target)
    d = diff_revisions(repo, cur, tgt)
    w = _vocabulary(repo, cur)
    software, rework, respin, other = [], [], [], []
    fp_changed = {x["ref"] for x in d["pcb"]["footprint_changes"]}

    for kind in ("added", "removed", "modified"):
        for a in d["artifacts"][kind]:
            if a["category"] in SOFTWARE_CATEGORIES:
                verb = {"added": "restore", "removed": "remove", "modified": "reflash"}[kind]
                software.append({"path": a["path"], "category": a["category"],
                                 "action": f"{verb} {a['path']} as in {tgt[:10]} (reprogram, no hardware change)"})
            elif a["category"] in ("pcb", "electronics"):
                continue  # covered by the semantic diff below
            else:
                other.append({"path": a["path"], "category": a["category"],
                              "action": f"{kind} {a['category']} artifact: no change to {w['carrier']} "
                                        "unless the part is physically affected"})
    for x in d["bom"]["replaced"]:
        old, new = x["old"], x["new"]
        if x["ref"] in fp_changed:
            respin.append({"ref": x["ref"], "kind": "replace_different_footprint",
                           "instruction": f"{x['ref']}: {w['fitting']} {old['footprint']} -> {new['footprint']}; "
                                          "cannot be reworked in place. Build a new PCB revision "
                                          "(or fit an adapter board if mechanically possible)."})
        else:
            rework.append({"ref": x["ref"], "kind": "replace",
                           "instruction": w["swap"].format(
                               ref=x["ref"], old=old["value"], new=new["value"],
                               mpn=new["mpn"] or new["lib_id"], fp=new["footprint"])})
    for x in d["bom"]["value_changed"]:
        rework.append({"ref": x["ref"], "kind": "value_change",
                       "instruction": f"Replace {x['ref']} {x['old_value']} with {x['new_value']}."})
    for x in d["bom"]["added"]:
        rework.append({"ref": x["ref"], "kind": "fit",
                       "instruction": w["refit"].format(ref=x["ref"], value=x["value"],
                                                        fp=x["footprint"])})
    for x in d["bom"]["removed"]:
        rework.append({"ref": x["ref"], "kind": "remove",
                       "instruction": f"Remove (depopulate) {x['ref']} ({x['value']})."})
    handled = {x["ref"] for x in respin}
    for x in d["pcb"]["footprint_changes"]:
        if x["ref"] not in handled:
            respin.append({"ref": x["ref"], "kind": "footprint_change",
                           "instruction": f"{x['ref']}: {w['fitting']} {x['old']} -> {x['new']} "
                                          f"{w['respin_verb']}."})
    for x in d["pcb"]["moved"]:
        respin.append({"ref": x["ref"], "kind": "placement",
                       "instruction": f"{x['ref']} placement differs ({x['distance_mm']} mm): "
                                      "layout-only change, applies to the next board build."})
    for n in d["nets"]["changed"]:
        respin.append({"ref": n["net"], "kind": "connectivity",
                       "instruction": f"Net {n['net']} connectivity differs: cut/jumper bodge wires "
                                      "or respin."})
    devices = [dv["serial"] for dv in list_devices(repo) if dv["revision"] != tgt]
    return {
        "from": cur, "to": tgt,
        "software_reversible": software, "physical_rework": rework, "respin_required": respin,
        "other": other,
        "fully_software_reversible": not (rework or respin),
        "vocabulary": w["respin"],
        "devices_not_at_target": devices,
    }


def render_plan(p: dict) -> str:
    L = [f"Rollback plan {p['from'][:10]} -> {p['to'][:10]}"]
    sections = (("Software-reversible (reflash/reprogram)", "software_reversible"),
                ("Physical rework required", "physical_rework"),
                (p.get("vocabulary") or _WORDS["board"]["respin"], "respin_required"),
                ("Other artifacts", "other"))
    for title, key in sections:
        if p[key]:
            L.append(f"{title}:")
            L += [f"  - {x.get('instruction') or x['action']}" for x in p[key]]
    if p["fully_software_reversible"]:
        L.append("Result: fully software-reversible.")
    else:
        L.append("Result: NOT fully software-reversible; hardware rework or respin needed.")
    if p["devices_not_at_target"]:
        L.append("Devices not at target revision: " + ", ".join(p["devices_not_at_target"]))
    return "\n".join(L)
