"""Engineering rules for datacenter nodes: power, cooling, PCIe and driver compatibility.

These are the node-domain counterparts of the board-level rules. The shapes are deliberately the
same: a power domain is a supply rail with a budget, an accelerator is a load with a rating, and
the question is always whether the thing supplying has enough for the things drawing.

Every finding produced here is ``rule_based``: it leans on catalog values and on declared
manifest numbers, both of which can be wrong or stale. TDP in particular is a board power rating,
not the draw of a specific workload.
"""
from __future__ import annotations

import re

RULE_BASED = "rule_based"
_REDUNDANCY = re.compile(r"^\s*(\d+)\s*\+\s*(\d+)\s*$")


def node_meta(design) -> dict:
    """The first node manifest in a design, or ``{}`` when the design is not a node."""
    nodes = (design.meta or {}).get("nodes") or []
    return nodes[0] if nodes else {}


def is_node_design(design) -> bool:
    return bool(node_meta(design))


def prop(component, part: dict | None, key: str, default=None):
    """Declared manifest value first, then the catalog, then ``default``.

    The manifest describes this node as built; the catalog describes the part in general. When a
    site has measured its own numbers, the manifest should win.
    """
    raw = (component.props or {}).get(key)
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return raw
    if part and part.get(key) not in (None, ""):
        return part[key]
    return default


def usable_watts(domain_cfg: dict, psu_watts: list[float]) -> tuple[float, str]:
    """Capacity of a PSU bank after honouring its redundancy policy.

    ``2+2`` means two supplies carry the load and two are spare, so the budget is two supplies'
    worth -- not the sum of all four. Getting this wrong is exactly the mistake that takes a rack
    down when one PSU fails.
    """
    total = sum(psu_watts)
    policy = str(domain_cfg.get("redundancy", "")).strip()
    m = _REDUNDANCY.match(policy)
    if m and psu_watts:
        active = int(m.group(1))
        each = domain_cfg.get("watts_each") or (total / len(psu_watts))
        return float(active) * float(each), f"{policy} redundancy"
    return total, "no declared redundancy"


def _version(text: str) -> tuple[int, ...] | None:
    """Numeric version tuple, or None when the string is not a plain dotted version."""
    text = str(text or "").strip()
    if not re.fullmatch(r"\d+(\.\d+)*", text):
        return None
    return tuple(int(x) for x in text.split("."))


def power_and_thermal_rules(b, d, meta: dict) -> list[str]:
    ids: list[str] = []
    changed = set(d["changed_refs"])
    domains = (meta.get("power") or {}).get("domains") or {}
    chassis = meta.get("chassis") or {}

    for domain, cfg in sorted(domains.items()):
        on_domain = [r for r, c in b.db.components.items() if domain in c.pads.values()]
        if not changed & set(on_domain):
            continue
        psus, loads, unknown = [], [], []
        for ref in sorted(on_domain):
            c = b.db.components[ref]
            part = b.catalog.lookup(c)
            kind = (c.lib_id or (part or {}).get("type") or "").lower()
            if kind == "psu":
                watts = prop(c, part, "watts")
                psus.append((ref, float(watts))) if watts else unknown.append(ref)
            else:
                tdp = prop(c, part, "tdp_w")
                loads.append((ref, float(tdp))) if tdp else unknown.append(ref)
        if not psus:
            continue

        overhead = float(cfg.get("overhead_w") or 0)
        draw = sum(w for _, w in loads) + overhead
        usable, policy = usable_watts(cfg, [w for _, w in psus])
        installed = sum(w for _, w in psus)
        ev = [{"type": "ref", "ref": r} for r, _ in psus[:2]] + [
            {"type": "declared", "source": "node manifest", "domain": domain,
             "redundancy": cfg.get("redundancy", ""), "overhead_w": overhead}]
        top = ", ".join(f"{r} {w:g} W" for r, w in sorted(loads, key=lambda x: -x[1])[:3])
        unknown_note = (f" Draw unknown for {', '.join(unknown)}, excluded from the total."
                        if unknown else "")
        text = (f"{domain}: {len(psus)} supplies totalling {installed:g} W installed, "
                f"{usable:g} W usable under {policy}. Declared draw {draw:g} W "
                f"({len(loads)} components plus {overhead:g} W overhead). Largest: {top}."
                f"{unknown_note}")

        if draw > usable:
            ids.append(b.add(RULE_BASED, "critical",
                             f"{domain} cannot supply the node under its redundancy policy",
                             text + f" Shortfall {draw - usable:g} W: losing a supply would drop "
                             "the node.", ev, [r for r, _ in psus]))
            b.retest("psu-redundancy-pull-test", RULE_BASED,
                     f"{domain} draw exceeds its redundant budget", ev)
        elif draw > 0.8 * usable:
            ids.append(b.add(RULE_BASED, "warning", f"{domain} has little power headroom",
                             text + f" That is {draw / usable:.0%} of the usable budget.", ev,
                             [r for r, _ in psus]))
        else:
            ids.append(b.add(RULE_BASED, "info", f"{domain} power budget adequate",
                             text + f" That is {draw / usable:.0%} of the usable budget.", ev))

        cooling = chassis.get("cooling_w")
        if cooling:
            cev = ev + [{"type": "declared", "source": "node manifest",
                         "chassis": chassis.get("model", "")}]
            if draw > float(cooling):
                ids.append(b.add(RULE_BASED, "warning", "Chassis cooling below declared draw",
                                 f"{chassis.get('model', 'chassis')} is rated {cooling:g} W of "
                                 f"cooling; declared draw is {draw:g} W. Expect throttling or "
                                 "elevated inlet temperatures under sustained load.", cev))
                b.retest("thermal-soak", RULE_BASED, "declared draw exceeds rated cooling", cev)
    return ids


def pcie_rules(b, d, meta: dict) -> list[str]:
    """Link generation and lane budget against the host root each device hangs off."""
    ids: list[str] = []
    changed = set(d["changed_refs"])
    hosts: dict[str, tuple[str, dict | None]] = {}
    for ref, c in b.db.components.items():
        part = b.catalog.lookup(c)
        if (c.lib_id or (part or {}).get("type") or "").lower() == "cpu":
            for net in c.pads.values():
                if "PCIE" in net.upper():
                    hosts[net] = (ref, part)

    per_root: dict[str, float] = {}
    # identical devices produce identical findings; group them so a 8-GPU node reports once
    downshifted: dict[tuple, list[str]] = {}
    for ref in sorted(b.db.components):
        c = b.db.components[ref]
        part = b.catalog.lookup(c)
        root = next((n for k, n in c.pads.items() if k.lower() == "pcie"), "")
        if not root or root not in hosts:
            continue
        lanes = prop(c, part, "pcie_lanes")
        if lanes and (c.lib_id or "").lower() != "cpu":
            per_root[root] = per_root.get(root, 0) + float(lanes)
        gen = prop(c, part, "pcie_gen")
        host_ref, host_part = hosts[root]
        host_gen = prop(b.db.components[host_ref], host_part, "pcie_gen")
        if ref in changed and gen and host_gen and float(gen) > float(host_gen):
            downshifted.setdefault((c.value, float(gen), float(host_gen)), []).append(ref)

    for (model, gen, host_gen), refs in sorted(downshifted.items()):
        who = refs[0] if len(refs) == 1 else f"{len(refs)} devices ({', '.join(refs)})"
        hosts_used = sorted({b.db.components[r].pads.get("pcie", "") for r in refs})
        host_refs = sorted({hosts[h][0] for h in hosts_used if h in hosts})
        ids.append(b.add(
            RULE_BASED, "warning",
            f"{model} negotiates down to the host PCIe generation"
            + (f" on {len(refs)} devices" if len(refs) > 1 else f" ({refs[0]})"),
            f"{who}: {model} is a PCIe Gen{gen:.0f} device, hosted at Gen{host_gen:.0f} by "
            f"{', '.join(host_refs)}. Links train at the lower generation, roughly halving host "
            "bandwidth per generation step. On-device compute is unaffected; host transfers and "
            "peer-to-peer staging are not.",
            b.ref_ev(refs[0]) + [{"type": "ref", "ref": r} for r in host_refs], refs + host_refs))

    for root, requested in sorted(per_root.items()):
        host_ref, host_part = hosts[root]
        available = prop(b.db.components[host_ref], host_part, "pcie_lanes")
        on_root = [r for r, c in b.db.components.items()
                   if c.pads.get("pcie") == root and r != host_ref]
        if not (changed & set(on_root)) or not available:
            continue
        if requested > float(available):
            ids.append(b.add(
                RULE_BASED, "warning", f"{root} lane budget oversubscribed",
                f"Devices on {root} request {requested:g} lanes; {host_ref} provides "
                f"{float(available):g}. Expect bifurcation, a switch, or devices running at "
                "reduced width.", [{"type": "ref", "ref": host_ref}], [host_ref]))
    return ids


def driver_rules(b, d, meta: dict) -> list[str]:
    """The installed accelerator driver against the minimum each installed part requires."""
    ids: list[str] = []
    changed = set(d["changed_refs"])
    firmware = meta.get("firmware") or {}
    installed = firmware.get("gpu_driver", "")
    have = _version(installed)
    needed: list[tuple[str, str, tuple[int, ...]]] = []
    unparsed: list[tuple[str, str]] = []
    for ref in sorted(changed):
        c = b.db.components.get(ref)
        if c is None:
            continue
        part = b.catalog.lookup(c)
        want = prop(c, part, "min_driver")
        if not want:
            continue
        parsed = _version(want)
        (needed.append((ref, str(want), parsed)) if parsed else unparsed.append((ref, str(want))))

    ev = [{"type": "declared", "source": "node manifest", "gpu_driver": installed,
           "cuda": firmware.get("cuda", "")}]
    if needed and have:
        worst = max(needed, key=lambda x: x[2])
        if worst[2] > have:
            refs = sorted({r for r, _, v in needed if v > have})
            ids.append(b.add(
                RULE_BASED, "critical", "Installed accelerator driver is below the required minimum",
                f"Node declares driver {installed}; {worst[0]} ({b.db.components[worst[0]].value}) "
                f"requires at least {worst[1]}. Affected: {', '.join(refs)}. These devices will "
                "not initialise until the driver is upgraded.",
                ev + [{"type": "ref", "ref": r} for r in refs[:4]], refs))
            b.retest("driver-upgrade-and-enumeration", RULE_BASED,
                     "installed driver below part minimum", ev)
        else:
            ids.append(b.add(RULE_BASED, "info", "Installed driver satisfies part minimums",
                             f"Driver {installed} meets the highest requirement "
                             f"({worst[1]}, from {worst[0]}).", ev))
    elif needed and not have:
        ids.append(b.add(RULE_BASED, "warning", "Installed driver version could not be compared",
                         f"Node declares {installed!r}, which is not a plain dotted version; the "
                         "minimum-driver check was skipped.", ev))
    for ref, want in unparsed:
        ids.append(b.add(RULE_BASED, "info", f"{ref} requires a non-numeric software stack",
                         f"Catalog records a minimum of {want!r}, which this check cannot compare "
                         "numerically. Verify it by hand.", b.ref_ev(ref), [ref]))
    return ids


def connector_rules(b, d, meta: dict) -> list[str]:
    """A changed power connector means a cable harness change, not a drop-in swap."""
    ids: list[str] = []
    swaps: dict[tuple[str, str], list[str]] = {}
    for item in d["bom"]["replaced"]:
        ref = item["ref"]
        new_c = b.db.components.get(ref)
        old_c = b.da.components.get(ref)
        if new_c is None or old_c is None:
            continue
        old = prop(old_c, b.catalog.lookup(old_c), "power_connector")
        new = prop(new_c, b.catalog.lookup(new_c), "power_connector")
        if old and new and str(old) != str(new):
            swaps.setdefault((str(old), str(new)), []).append(ref)

    for (old, new), refs in sorted(swaps.items()):
        count = f" on {len(refs)} devices" if len(refs) > 1 else f" ({refs[0]})"
        ev = b.ref_ev(refs[0])
        ids.append(b.add(
            RULE_BASED, "warning", f"Power connector changed: {old} -> {new}{count}",
            f"Affected: {', '.join(refs)}. The existing cable harness does not fit. Confirm the "
            "supply side provides the new connector, that the loom has enough of them, and that "
            "any adapter is rated for the part's full draw.", ev, refs))
        b.retest("power-harness-fit-check", RULE_BASED,
                 f"connector type changed on {len(refs)} device(s)", ev)
    return ids


def datacenter_rules(b, d) -> list[str]:
    """Every node-domain rule. Returns the ids of the findings produced."""
    meta = node_meta(b.db)
    if not meta:
        return []
    ids = power_and_thermal_rules(b, d, meta)
    ids += pcie_rules(b, d, meta)
    ids += driver_rules(b, d, meta)
    ids += connector_rules(b, d, meta)
    return ids
