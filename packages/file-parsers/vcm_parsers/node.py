"""Node manifests: a server or rack node described as components and the domains they share.

A datacenter node is the same shape of problem as a circuit board. Accelerators, CPUs, DIMMs,
NICs and power supplies are components; the things they share are power domains, PCIe roots and
interconnect fabrics. Expressed that way, the existing diff, graph and impact engines work on a
GPU chassis without changing a line: a power domain behaves exactly as a supply rail does, and
"which PSU bank feeds these eight accelerators" is the same query as "what sits on +3V3".

Manifest shape (``*.node.json``)::

    {
      "schema": "vcm-node/1",
      "node": "gpu-node-01",
      "chassis": {"model": "...", "rack_units": 4, "cooling_w": 3500},
      "power": {"domains": {"PSU_BANK": {"redundancy": "2+2"}}},
      "firmware": {"gpu_driver": "535.129.03", "cuda": "12.2", "bios": "..."},
      "components": [
        {"ref": "GPU0", "type": "gpu", "model": "NVIDIA H100 NVL", "mpn": "H100-NVL-94GB",
         "slot": "PCIE_SLOT1", "form_factor": "FHFL dual-slot",
         "connections": {"power": "PSU_BANK", "pcie": "CPU0_PCIE_ROOT"}}
      ]
    }

Anything not listed above is carried through into ``Component.props`` untouched, so a site can
declare its own fields without the parser needing to know about them.
"""
from __future__ import annotations

import json

from .model import Component

SCHEMA = "vcm-node/1"
#: keys lifted onto the Component itself; everything else lands in props
_RESERVED = {"ref", "type", "model", "mpn", "manufacturer", "description", "slot", "form_factor",
             "connections"}


def looks_like_node(text: str) -> bool:
    """Cheap check before paying for a full parse."""
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and str(data.get("schema", "")).startswith("vcm-node/")


def _object(value, where: str) -> dict:
    """A JSON object, or a ValueError that says where the manifest has the wrong shape."""
    if not isinstance(value, dict):
        raise ValueError(f"node manifest: {where} must be an object, not {type(value).__name__}")
    return value


def parse_node(text: str) -> tuple[list[Component], set[str], list[str], dict]:
    """Return ``(components, declared_domains, duplicate_refs, meta)`` for one node manifest."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("node manifest must be a JSON object")
    comps: list[Component] = []
    seen: set[str] = set()
    dups: list[str] = []
    power = _object(data.get("power", {}), "power")
    domains: set[str] = set(_object(power.get("domains", {}), "power.domains"))
    entries = data.get("components", [])
    if not isinstance(entries, list):
        raise ValueError("node manifest: components must be a list")

    for i, entry in enumerate(entries):
        entry = _object(entry, f"components[{i}]")
        ref = str(entry.get("ref", "")).strip()
        if not ref:
            continue
        if ref in seen:
            dups.append(ref)
            continue
        seen.add(ref)
        conns = _object(entry.get("connections") or {}, f"components[{i}].connections")
        connections = {str(k): str(v) for k, v in conns.items() if v}
        domains |= set(connections.values())
        slot, form = str(entry.get("slot", "")), str(entry.get("form_factor", ""))
        props = {str(k): str(v) for k, v in entry.items() if k not in _RESERVED}
        if slot:
            props.setdefault("slot", slot)
        comps.append(Component(
            ref=ref,
            value=str(entry.get("model", "")),
            # the slot and form factor play the role a footprint does on a board: changing them
            # means physical rework, so the existing footprint-change finding applies unchanged
            footprint=" / ".join(x for x in (slot, form) if x),
            lib_id=str(entry.get("type", "")),
            mpn=str(entry.get("mpn", "")),
            manufacturer=str(entry.get("manufacturer", "")),
            description=str(entry.get("description", "")),
            pads=connections,
            props=props))

    meta = {k: v for k, v in data.items() if k != "components"}
    return comps, domains, dups, meta
