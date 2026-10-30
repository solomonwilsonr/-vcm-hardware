"""Deterministic, offline impact analysis.

Every finding has exactly one ``confidence_class``:
  verified     facts derived from parsed design data, declared project links or stored evidence
  rule_based   heuristic warnings from a small component-property DB and engineering rules
  speculative  predictions that follow from rule-based findings but are not established
"""
from __future__ import annotations

import json
import re
from importlib import resources

from vcm_diff import diff_designs
from vcm_graph import build_graph, project_config
from vcm_parsers import Component, Design, design_from_revision
from vcm_validation import compare_validation

from .provider import LLMProvider, NoopProvider

VERIFIED, RULE_BASED, SPECULATIVE = "verified", "rule_based", "speculative"
CLASSES = (VERIFIED, RULE_BASED, SPECULATIVE)
AMBIENT_C = 40.0
_PARTS: list[dict] | None = None


def load_parts() -> list[dict]:
    global _PARTS
    if _PARTS is None:
        text = resources.files("vcm_ai").joinpath("data/components.json").read_text()
        _PARTS = json.loads(text)["parts"]
    return _PARTS


def lookup(c: Component) -> dict | None:
    keys = [s.upper() for s in (c.mpn, c.value) if s]
    for part in load_parts():
        if any(k.startswith(m.upper()) for k in keys for m in part["match"]):
            return part
    return None


def parse_uf(value: str) -> float | None:
    m = re.fullmatch(r"\s*([\d.]+)\s*([pnuµm]?)F?\s*", value or "", re.I)
    if not m:
        return None
    return float(m.group(1)) * {"p": 1e-6, "n": 1e-3, "u": 1.0, "µ": 1.0, "m": 1e3, "": 1e6}[m.group(2).lower()]


def parse_volts(text: str) -> float | None:
    m = re.fullmatch(r"\s*\+?(\d+)V(\d*)\s*", text or "", re.I)
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2)}") if m.group(2) else float(m.group(1))


class _Builder:
    def __init__(self, repo, a, b, da: Design, db: Design, cfg: dict):
        self.repo, self.a, self.b, self.da, self.db, self.cfg = repo, a, b, da, db, cfg
        self.findings: list[dict] = []
        self.retests: list[dict] = []
        self._n = {c: 0 for c in CLASSES}

    # -- evidence helpers --
    def commit_ev(self) -> list[dict]:
        return [{"type": "commit", "revision": self.a.id, "role": "base"},
                {"type": "commit", "revision": self.b.id, "role": "head"}]

    def ref_ev(self, ref: str, design: Design | None = None, rev=None) -> list[dict]:
        design, rev = design or self.db, rev or self.b
        ev: list[dict] = [{"type": "ref", "ref": ref}]
        comp = design.components.get(ref)
        if comp:
            for kind, paths in design.sources.items():
                if kind in comp.by_source:
                    for p in paths:
                        ev.append({"type": "file", "path": p, "hash": rev.artifacts[p].hash,
                                   "revision": rev.id})
        return ev

    def file_ev(self, path: str) -> dict | None:
        art = self.b.artifacts.get(path)
        return {"type": "file", "path": path, "hash": art.hash, "revision": self.b.id} if art else None

    def add(self, cls: str, severity: str, title: str, detail: str, evidence: list[dict],
            refs: list[str] | None = None, derived_from: list[str] | None = None) -> str:
        self._n[cls] += 1
        fid = {"verified": "V", "rule_based": "R", "speculative": "S"}[cls] + f"{self._n[cls]:03d}"
        f = {"id": fid, "confidence_class": cls, "severity": severity, "title": title,
             "detail": detail, "refs": refs or [], "evidence": evidence + self.commit_ev()}
        if derived_from:
            f["derived_from"] = derived_from
        self.findings.append(f)
        return fid

    def retest(self, name: str, cls: str, reason: str, evidence: list[dict]) -> None:
        if not any(r["name"] == name for r in self.retests):
            self.retests.append({"name": name, "confidence_class": cls, "reason": reason,
                                 "evidence": evidence})

    # -- electrical helpers --
    def rail_voltage(self, net: str) -> float | None:
        rails = self.cfg.get("rails", {})
        return rails[net] if net in rails else parse_volts(net)

    @staticmethod
    def net_for(c: Component, part: dict, function: str) -> str:
        for pad, fn in part.get("pinout", {}).items():
            if fn == function and c.pads.get(pad):
                return c.pads[pad]
        return ""

    def loads_on(self, net: str, exclude: str) -> tuple[float, float, list[str], list[str]]:
        typ = peak = 0.0
        used, unknown = [], []
        ext = self.cfg.get("external_loads", {})
        for ref, c in self.db.components.items():
            if ref == exclude or net not in c.pads.values():
                continue
            part = lookup(c)
            if ref in ext:
                typ += ext[ref].get("typ_ma", 0)
                peak += ext[ref].get("peak_ma", 0)
                used.append(ref)
            elif part and "i_typ_ma" in part:
                typ += part["i_typ_ma"]
                peak += part.get("i_peak_ma", part["i_typ_ma"])
                used.append(ref)
            elif not part and not ref.startswith(("C", "R", "D")):
                unknown.append(ref)
        return typ, peak, used, unknown


def _verified_diff_findings(b: _Builder, d: dict) -> None:
    for x in d["bom"]["replaced"]:
        o, n = x["old"], x["new"]
        b.add(VERIFIED, "warning", f"{x['ref']} replaced: {o['value']} -> {n['value']}",
              f"Changed fields: {', '.join(x['changed_fields'])}. Old part {o['mpn'] or o['lib_id']}, "
              f"new part {n['mpn'] or n['lib_id']}.", b.ref_ev(x["ref"]), [x["ref"]])
    for x in d["bom"]["value_changed"]:
        b.add(VERIFIED, "info", f"{x['ref']} value changed: {x['old_value']} -> {x['new_value']}",
              "Same part identity, different value.", b.ref_ev(x["ref"]), [x["ref"]])
    for x in d["bom"]["added"]:
        b.add(VERIFIED, "info", f"{x['ref']} added ({x['value']})", x["footprint"],
              b.ref_ev(x["ref"]), [x["ref"]])
    for x in d["bom"]["removed"]:
        b.add(VERIFIED, "warning", f"{x['ref']} removed ({x['value']})", x["footprint"],
              b.ref_ev(x["ref"], b.da, b.a), [x["ref"]])
    for x in d["pcb"]["footprint_changes"]:
        b.add(VERIFIED, "warning", f"{x['ref']} footprint changed: {x['old']} -> {x['new']}",
              "PCB layout must change: this is not a drop-in swap on the existing board.",
              b.ref_ev(x["ref"]), [x["ref"]])
    for x in d["pcb"]["moved"]:
        b.add(VERIFIED, "info", f"{x['ref']} moved {x['distance_mm']} mm",
              f"({x['old']['x']}, {x['old']['y']}) -> ({x['new']['x']}, {x['new']['y']})",
              b.ref_ev(x["ref"]), [x["ref"]])
    for net in d["nets"]["changed"]:
        b.add(VERIFIED, "warning", f"Net {net['net']} connectivity changed",
              f"pins added {net['pins_added']}, removed {net['pins_removed']}",
              [{"type": "ref", "ref": p.split('.')[0]} for p in net["pins_added"] + net["pins_removed"]],
              sorted({p.split('.')[0] for p in net["pins_added"] + net["pins_removed"]}))
    for name in d["nets"]["added"] + d["nets"]["removed"]:
        b.add(VERIFIED, "warning", f"Net {name} added/removed", "Netlist topology changed.",
              [{"type": "ref", "ref": name}])


def _verified_graph_findings(b: _Builder, d: dict) -> None:
    graph = build_graph(b.repo, b.b.id, b.db)
    seen_fw: set[str] = set()
    for ref in d["changed_refs"]:
        if ref not in b.db.components:
            continue
        aff = graph.affected_by(ref)
        if aff["components"]:
            names = ", ".join(c["ref"] for c in aff["components"])
            nets = sorted({n for c in aff["components"] for n in c["via_nets"]})
            b.add(VERIFIED, "info", f"{ref} shares nets with {len(aff['components'])} components",
                  f"Via nets {', '.join(nets)}: {names}. (ground nets are not followed)",
                  b.ref_ev(ref) + [{"type": "ref", "ref": c["ref"]} for c in aff["components"]],
                  [ref] + [c["ref"] for c in aff["components"]])
        for fw in aff["firmware"]:
            if fw["node"] in seen_fw:
                continue
            seen_fw.add(fw["node"])
            path = fw["node"].split(":", 1)[1]
            b.add(VERIFIED, "warning", f"Firmware {path} is linked to affected components",
                  f"vcm.json declares {', '.join(v.split(':')[1] for v in fw['via'])} configured_by {path}; "
                  f"change to {ref} reaches it through the netlist.",
                  [e for e in (b.file_ev("vcm.json"), b.file_ev(path)) if e] + [{"type": "ref", "ref": ref}],
                  [ref])
        for t in aff["tests"]:
            name = t["node"].split(":", 1)[1]
            b.retest(name, VERIFIED, f"validates {', '.join(v.split(':')[1] for v in t['via'])} "
                     f"(affected by change to {ref})",
                     [e for e in (b.file_ev("vcm.json"),) if e] + [{"type": "ref", "ref": ref}])


def _regulator_rules(b: _Builder, d: dict) -> list[str]:
    changed = set(d["changed_refs"])
    ids: list[str] = []
    for ref, c in b.db.components.items():
        part = lookup(c)
        if not part or part["type"] not in ("ldo", "buck"):
            continue
        vin_net, vout_net = b.net_for(c, part, "VIN"), b.net_for(c, part, "VOUT")
        cap_refs = [r for r, x in b.db.components.items()
                    if r != ref and vout_net in x.pads.values() and r.startswith("C")]
        if not ({ref} | set(cap_refs)) & changed:
            continue
        ev = b.ref_ev(ref) + [{"type": "db", "part": part["match"][0]}]
        old_c = b.da.components.get(ref)
        old_part = lookup(old_c) if old_c else None
        # R: output current capability
        typ, peak, used, unknown = b.loads_on(vout_net, ref)
        imax = part["iout_max_ma"]
        was = f" (was {old_part['iout_max_ma']} mA)" if old_part and old_part is not part else ""
        note = f" Unknown loads ignored: {', '.join(unknown)}." if unknown else ""
        text = (f"{ref} ({c.value}) is rated {imax} mA{was}; loads on {vout_net} "
                f"({', '.join(used)}) total {typ:.0f} mA typical / {peak:.0f} mA peak.{note}")
        if peak > imax:
            ids.append(b.add(RULE_BASED, "critical", f"{ref} cannot supply peak load on {vout_net}", text,
                             ev, [ref] + used))
            b.retest("peak-load stress test", RULE_BASED, f"{ref} rating below peak load", ev)
        elif typ > 0.8 * imax:
            ids.append(b.add(RULE_BASED, "warning", f"{ref} has little current headroom", text, ev, [ref] + used))
        else:
            ids.append(b.add(RULE_BASED, "info", f"{ref} current headroom adequate", text, ev, [ref] + used))
        # R: output voltage vs load supply range
        for r in used:
            lp = lookup(b.db.components[r])
            if lp and "supply_vmin" in lp and not lp["supply_vmin"] <= part["vout"] <= lp["supply_vmax"]:
                ids.append(b.add(RULE_BASED, "critical", f"{ref} output outside {r} supply range",
                                 f"{part['vout']} V vs {lp['supply_vmin']}-{lp['supply_vmax']} V", ev, [ref, r]))
        # R: input headroom and thermal
        vin = b.rail_voltage(vin_net) if vin_net else None
        if vin is not None:
            if vin > part["vin_max"]:
                ids.append(b.add(RULE_BASED, "critical", f"{ref} input exceeds maximum",
                                 f"{vin} V on {vin_net}, limit {part['vin_max']} V", ev, [ref]))
            elif vin < part["vout"] + part["dropout_v"]:
                ids.append(b.add(RULE_BASED, "warning", f"{ref} dropout margin insufficient",
                                 f"{vin} V in, {part['vout']} V out needs {part['vout'] + part['dropout_v']:.2f} V",
                                 ev, [ref]))
            drop = vin - part["vout"]
            tj_typ = AMBIENT_C + drop * typ / 1000 * part["rth_ja_c_per_w"]
            tj_peak = AMBIENT_C + drop * peak / 1000 * part["rth_ja_c_per_w"]
            old_txt = ""
            if old_part and old_part is not part:
                old_peak = AMBIENT_C + drop * peak / 1000 * old_part["rth_ja_c_per_w"]
                old_txt = f" Previous part at the same load: {old_peak:.0f} C peak."
            msg = (f"Estimated junction temperature at {AMBIENT_C:.0f} C ambient: {tj_typ:.0f} C typical, "
                   f"{tj_peak:.0f} C at peak load (Rth_JA {part['rth_ja_c_per_w']} C/W, "
                   f"{drop:.1f} V drop, limit {part['tj_max_c']} C).{old_txt}")
            if tj_typ > part["tj_max_c"]:
                ids.append(b.add(RULE_BASED, "critical", f"{ref} thermal limit exceeded (typical load)", msg, ev, [ref]))
            elif tj_peak > part["tj_max_c"]:
                ids.append(b.add(RULE_BASED, "warning", f"{ref} exceeds thermal limit at peak load", msg, ev, [ref]))
            b.retest("thermal soak at maximum load", RULE_BASED, f"{ref} power dissipation changed", ev)
        # R: output capacitor compatibility
        total_uf = sum(parse_uf(b.db.components[r].value) or 0 for r in cap_refs)
        ceramic = [r for r in cap_refs if b.db.components[r].footprint.startswith("Capacitor_SMD")]
        if part["output_cap"] == "requires_esr" and ceramic and len(ceramic) == len(cap_refs):
            ids.append(b.add(RULE_BASED, "warning", f"{ref} expects an ESR-stabilised output capacitor",
                             f"Output cap(s) {', '.join(ceramic)} are ceramic; this LDO family may oscillate "
                             "with very low ESR.", ev + b.ref_ev(ceramic[0]), [ref] + ceramic))
        if total_uf < part["min_cout_uf"]:
            ids.append(b.add(RULE_BASED, "warning", f"{ref} output capacitance below recommended minimum",
                             f"{total_uf:g} uF on {vout_net}, datasheet-class minimum {part['min_cout_uf']} uF",
                             ev, [ref] + cap_refs))
        # R: pinout consistency and package match
        if old_c and old_part:
            mismatch = False
            for fn in ("VIN", "VOUT", "GND"):
                on, nn = b.net_for(old_c, old_part, fn), b.net_for(c, part, fn)
                if on != nn:
                    mismatch = True
                    ids.append(b.add(RULE_BASED, "critical", f"{ref} {fn} connects to a different net",
                                     f"{fn}: {on or '-'} before, {nn or '-'} now", ev, [ref]))
            if not mismatch:
                ids.append(b.add(RULE_BASED, "info", f"{ref} VIN/VOUT/GND nets consistent with part pinout",
                                 "Pad-to-function mapping per the component DB keeps the same nets as before.",
                                 ev, [ref]))
        if part["package"].upper() not in c.footprint.upper():
            ids.append(b.add(RULE_BASED, "warning", f"{ref} footprint does not match package {part['package']}",
                             c.footprint, ev, [ref]))
        b.retest("power-up and brownout test", RULE_BASED, f"regulator {ref} changed", ev)
    return ids


def _cap_rating_rules(b: _Builder, d: dict) -> None:
    for ref in d["changed_refs"]:
        c = b.db.components.get(ref)
        if not c or not ref.startswith("C") or not c.voltage:
            continue
        rating = parse_volts(c.voltage)
        volts = [v for n in set(c.pads.values()) if (v := b.rail_voltage(n))]
        if rating and volts and rating < 1.5 * max(volts):
            b.add(RULE_BASED, "warning", f"{ref} voltage rating has little derating margin",
                  f"{c.voltage} rated on a {max(volts):g} V rail; 1.5x derating guideline suggests "
                  f">= {1.5 * max(volts):.1f} V for ceramics.",
                  b.ref_ev(ref), [ref])


def _firmware_rules(b: _Builder, d: dict, reg_ids: list[str]) -> list[str]:
    ids: list[str] = []
    if not reg_ids:
        return ids
    for link in b.cfg.get("firmware_links", []):
        comp = link.get("component")
        if comp not in b.db.components:
            continue
        lines = []
        for path in link.get("paths", []):
            if path in b.b.artifacts:
                text = b.repo.read_artifact(b.b, path).decode("utf-8", "replace")
                lines += [f"{path}: {ln.strip()}" for ln in text.splitlines()
                          if re.search(r"BROWNOUT|VDD|VOLT|SUPPLY", ln, re.I) and ln.strip().startswith("#define")]
        ev = [e for e in (b.file_ev(p) for p in link.get("paths", [])) if e]
        ids.append(b.add(
            RULE_BASED, "warning", f"Firmware for {comp} may depend on the changed supply",
            "Supply-related settings should be reviewed against the new regulator"
            + (": " + "; ".join(lines) if lines else "."), ev + b.ref_ev(comp), [comp], reg_ids[:1]))
        b.retest("firmware brownout/reset behaviour check", RULE_BASED,
                 f"{comp} firmware references supply settings", ev)
    return ids


def _speculative(b: _Builder, d: dict, reg_ids: list[str]) -> None:
    by_id = {f["id"]: f for f in b.findings}
    crit = [i for i in reg_ids if by_id[i]["severity"] == "critical"]
    for fid in crit:
        if "peak load" in by_id[fid]["title"]:
            b.add(SPECULATIVE, "warning", "Radio transmit bursts may brown out or reset units",
                  "If peak demand exceeds the regulator rating, the rail may droop during TX bursts; "
                  "this is a prediction, not a measurement.", by_id[fid]["evidence"][:1], by_id[fid]["refs"], [fid])
            break
    for fid in reg_ids:
        if "thermal" in by_id[fid]["title"]:
            b.add(SPECULATIVE, "info", "Enclosure heating may shift thermal margin in the field",
                  "Thermal estimates assume still air and a fixed Rth_JA; real boards differ.",
                  by_id[fid]["evidence"][:1], by_id[fid]["refs"], [fid])
            break
    for x in d["bom"]["replaced"]:
        old = b.da.components[x["ref"]] if x["ref"] in b.da.components else None
        if x["ref"].startswith("C") and old and old.footprint.startswith("Capacitor_Tantalum") \
                and b.db.components[x["ref"]].footprint.startswith("Capacitor_SMD"):
            b.add(SPECULATIVE, "info", f"Moving {x['ref']} from tantalum to ceramic may change rail ripple/stability",
                  "ESR drops and DC-bias reduces effective capacitance; effect on regulator loop is unmeasured.",
                  b.ref_ev(x["ref"]), [x["ref"]])


def analyze(repo, rev_a: str, rev_b: str, provider: LLMProvider | None = None) -> dict:
    """Impact analysis from rev_a to rev_b. Fully offline unless a non-default provider is passed."""
    a, bb = repo.get_revision(rev_a), repo.get_revision(rev_b)
    da, db = design_from_revision(repo, a), design_from_revision(repo, bb)
    d = diff_designs(da, db)
    b = _Builder(repo, a, bb, da, db, project_config(repo, bb.id))
    _verified_diff_findings(b, d)
    _verified_graph_findings(b, d)
    try:
        v = compare_validation(repo, a.id, bb.id)
        b.add(VERIFIED, "critical" if v["regression"] else "info",
              f"Validation {v['status']['from']} -> {v['status']['to']}",
              f"errors {v['counts']['from']['error']} -> {v['counts']['to']['error']}, "
              f"warnings {v['counts']['from']['warning']} -> {v['counts']['to']['warning']}; "
              f"{len(v['new_issues'])} new, {len(v['resolved_issues'])} resolved",
              [])
    except Exception as exc:  # validation evidence is advisory for the impact report
        b.add(VERIFIED, "info", "Validation comparison unavailable", str(exc), [])
    reg_ids = _regulator_rules(b, d)
    _cap_rating_rules(b, d)
    _firmware_rules(b, d, reg_ids)
    _speculative(b, d, reg_ids)
    provider = provider or NoopProvider()
    summary_lines = [f"{f['id']} {f['title']}" for f in b.findings if f["confidence_class"] != SPECULATIVE]
    text = provider.complete("Summarise hardware change impact:\n" + "\n".join(summary_lines))
    if text:
        b.add(SPECULATIVE, "info", f"LLM commentary ({provider.name})", text, [{"type": "ref", "ref": provider.name}])
    counts = {c: sum(f["confidence_class"] == c for f in b.findings) for c in CLASSES}
    sev = {s: sum(f["severity"] == s for f in b.findings) for s in ("critical", "warning", "info")}
    return {"from": a.id, "to": bb.id, "provider": provider.name,
            "summary": {"by_class": counts, "by_severity": sev, "changed_components": d["changed_refs"]},
            "findings": b.findings, "required_retests": b.retests, "diff_summary": d["summary"]}
