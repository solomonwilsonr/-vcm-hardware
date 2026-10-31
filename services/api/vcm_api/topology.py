"""One read-only picture of a setup: components, how they connect, what the machine says, and
which units and racks carry them.

Everything here is derived from the repository and the evidence stored with a revision. Nothing is
measured live: "live" on the dashboard means the page re-reads this view as the repository
changes, and ``fingerprint`` lets it tell when something did.
"""
from __future__ import annotations

import hashlib
import json
import re

from vcm_core import Repository
from vcm_parsers import design_from_revision

# the port a component declares a connection on says what kind of domain it is
_PORT_KIND = {"power": "power", "pcie": "pcie", "interconnect": "fabric", "fabric": "fabric",
              "network": "fabric", "ethernet": "fabric", "memory": "memory", "mem": "memory"}
_SHOWN_PROPS = ("tdp_w", "pcie_gen", "pcie_lanes", "power_connector", "capacity_gb", "memory_type",
                "cores", "watts", "speed")
_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


def _natural(ref: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


def _status(ref: str, ctype: str, obs: dict | None, hits: dict[str, list[dict]]) -> str:
    if obs is None:
        return "unobserved"
    if ref in hits:
        return "drift"
    return "matches" if ctype in set(obs.get("covers", [])) else "unchecked"


def _measured(obs: dict, refs: set[str]) -> dict[str, float]:
    """Latest sampled draw per GPU, only for GPUs the manifest actually names ``GPU<index>``."""
    out: dict[str, float] = {}
    for idx, samples in ((obs.get("telemetry") or {}).get("gpu_power_w") or {}).items():
        ref = f"GPU{idx}"
        if ref in refs and samples:
            out[ref] = float(samples[-1])
    return out


def build_topology(repo: Repository, rev: str = "HEAD") -> dict:
    rid = repo.resolve(rev)
    revision = repo.get_revision(rid)
    design = design_from_revision(repo, revision)
    node_meta = (design.meta.get("nodes") or [{}])[0]

    ev = repo.latest_evidence(rid, "observed")
    obs = (ev or {}).get("observation") or None
    drift = (ev or {}).get("drift") or {}
    hits: dict[str, list[dict]] = {}
    for f in drift.get("findings", []):
        for ref in f.get("refs") or []:
            hits.setdefault(ref, []).append({"severity": f.get("severity", "info"),
                                             "kind": f.get("kind", ""), "title": f.get("title", "")})
    measured = _measured(obs, set(design.components)) if obs else {}

    components, domains = [], {}
    for ref in sorted(design.components, key=_natural):
        c = design.components[ref]
        ctype = c.lib_id or "part"
        slot = c.props.get("slot", "")
        form = c.footprint.split(" / ", 1)[1] if " / " in c.footprint else ""
        for port, dom in c.pads.items():
            if not dom:
                continue
            d = domains.setdefault(dom, {"name": dom, "kinds": {}, "members": []})
            d["kinds"][_PORT_KIND.get(port.lower(), "other")] = \
                d["kinds"].get(_PORT_KIND.get(port.lower(), "other"), 0) + 1
            d["members"].append(ref)
        found = hits.get(ref, [])
        components.append({
            "ref": ref, "type": ctype, "model": c.value, "mpn": c.mpn,
            "manufacturer": c.manufacturer, "slot": slot, "form_factor": form,
            "connections": {p: d for p, d in c.pads.items() if d},
            "props": {k: c.props[k] for k in _SHOWN_PROPS if k in c.props},
            "status": _status(ref, ctype, obs, hits),
            "findings": found, "measured_w": measured.get(ref)})

    power = (node_meta.get("power") or {}).get("domains") or {}
    dom_list = []
    for name, d in sorted(domains.items()):
        kind = max(d["kinds"], key=d["kinds"].get) if d["kinds"] else "other"
        dom_list.append({"name": name, "kind": "power" if name in power else kind,
                         "members": sorted(set(d["members"]), key=_natural),
                         "declared": power.get(name) or {}})

    devices = [{"serial": d["serial"], "release": d.get("release", ""),
                "revision": d.get("revision", ""),
                "on_revision": d.get("revision") == rid,
                "rack": (d.get("location") or {}).get("rack"),
                "position": (d.get("location") or {}).get("position")}
               for d in repo.list_records("devices")]
    racks = []
    for r in repo.list_records("racks"):
        placed = sorted((x for x in devices if x["rack"] == r["name"]),
                        key=lambda x: (x["position"] is None, x["position"] or 0))
        racks.append({"name": r["name"], "site": r.get("site", ""), "units": r.get("units"),
                      "power_budget_w": r.get("power_budget_w"),
                      "placed": [{"serial": x["serial"], "position": x["position"]} for x in placed]})

    # findings about the node as a whole (firmware, driver) name no component, so they are listed
    # beside the map rather than pinned to a part
    node_findings = [{"severity": f.get("severity", "info"), "title": f.get("title", ""),
                      "detail": f.get("detail", "")}
                     for f in drift.get("findings", []) if not f.get("refs")]
    observed = None
    if obs is not None:
        node_w = (obs.get("telemetry") or {}).get("node_power_w") or []
        observed = {"captured_at": obs.get("captured_at", ""), "sources": obs.get("sources", []),
                    "covers": obs.get("covers", []), "status": drift.get("status", ""),
                    "counts": drift.get("counts", {}), "unchecked_types": drift.get("unchecked_types", []),
                    "firmware": obs.get("firmware", {}),
                    "node_findings": sorted(node_findings, key=lambda f: -_SEVERITY_RANK.get(f["severity"], 0)),
                    "node_power_w": float(node_w[-1]) if node_w else None}
    body = {
        "schema": "vcm-topology/1",
        "revision": {"id": rid, "short": rid[:10], "message": revision.message,
                     "author": revision.author, "timestamp": revision.timestamp,
                     "tags": sorted(t for t, i in repo.tags().items() if i == rid)},
        "node": {"name": node_meta.get("node", ""), "site": node_meta.get("site", ""),
                 "chassis": node_meta.get("chassis") or {}, "firmware": node_meta.get("firmware") or {}},
        "components": components, "domains": dom_list, "devices": devices, "racks": racks,
        "observed": observed,
        "counts": {"components": len(components),
                   "by_type": {t: sum(c["type"] == t for c in components)
                               for t in sorted({c["type"] for c in components})},
                   "drift": sum(c["status"] == "drift" for c in components)},
    }
    body["fingerprint"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return body
