"""NetBox import: take rack placement from the system that already owns it.

NetBox is usually the source of truth for *where* a unit is; VCM is the source of truth for *what*
it is and what a change to it breaks. This reads NetBox's ``/api/dcim/devices/`` and
``/api/dcim/racks/`` JSON (saved to a file, or fetched by ``vcm fleet netbox --url``) and updates
placement on units VCM already knows, matched by serial number first and device name second.

Nothing is registered from NetBox: a unit without a release is not something VCM can reason
about, so unmatched NetBox devices are reported, not invented.
"""
from __future__ import annotations

from vcm_core import Repository

from .devices import list_devices
from .fleet import define_rack, list_racks, place_device


def _results(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    return list((payload or {}).get("results") or [])


def _name(obj) -> str:
    if isinstance(obj, dict):
        return str(obj.get("name") or obj.get("display") or "")
    return str(obj or "")


def import_netbox(repo: Repository, devices_payload, racks_payload=None,
                  budget_field: str = "power_budget_w") -> dict:
    racks_known = {r["name"] for r in list_racks(repo)}
    racks_created, racks_updated = [], []
    for rack in _results(racks_payload):
        name = _name(rack)
        if not name:
            continue
        cf = rack.get("custom_fields") or {}
        budget = cf.get(budget_field)
        define_rack(repo, name, float(budget) if budget not in (None, "") else None,
                    site=_name(rack.get("site")), units=rack.get("u_height"))
        (racks_updated if name in racks_known else racks_created).append(name)
        racks_known.add(name)

    units = {d["serial"]: d for d in list_devices(repo)}
    by_lower = {s.lower(): s for s in units}
    placed, unmatched, unplaced = [], [], []
    for nb in _results(devices_payload):
        serial = str(nb.get("serial") or "").strip()
        name = str(nb.get("name") or "").strip()
        match = by_lower.get(serial.lower()) if serial else None
        match = match or (by_lower.get(name.lower()) if name else None)
        if match is None:
            unmatched.append({"name": name, "serial": serial})
            continue
        rack = _name(nb.get("rack"))
        if not rack:
            unplaced.append(match)
            continue
        position = nb.get("position")
        place_device(repo, match, rack, int(position) if position not in (None, "") else None)
        placed.append({"serial": match, "rack": rack, "position": position})
        if rack not in racks_known:
            racks_created.append(rack)
            racks_known.add(rack)
    seen = {p["serial"] for p in placed} | set(unplaced)
    repo.audit.append("netbox.import", "fleet", {"placed": len(placed),
                                                 "unmatched": len(unmatched)})
    return {"placed": placed, "unmatched_in_netbox": unmatched,
            "no_rack_in_netbox": unplaced,
            "not_in_netbox": sorted(s for s in units if s not in seen),
            "racks_created": racks_created, "racks_updated": racks_updated}
