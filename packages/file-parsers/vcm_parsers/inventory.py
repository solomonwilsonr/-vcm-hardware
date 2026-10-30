"""Observed inventory: what a machine reports about itself, normalised into one shape.

A node manifest says what a node *should* be. These parsers read what it *is*, from captures of
the management controller (Redfish) and of the accelerator driver (``nvidia-smi``), so the two can be
compared. Parsing is offline and works on saved captures; fetching a live capture is a separate,
explicitly invoked step (``vcm inventory fetch-redfish``).

Observation shape (``vcm-observed/1``)::

    {"schema": "vcm-observed/1", "node": "gpu-node-01", "sources": ["redfish", "nvidia-smi"],
     "covers": ["cpu", "dimm", "gpu", "psu"],            # types the sources can see at all
     "components": [{"type": "gpu", "model": "...", "mpn": "...", "serial": "...",
                     "slot": "...", "props": {...}}],
     "firmware": {"bios": "3.4", "bmc": "1.12.03", "gpu_driver": "535.129.03"},
     "telemetry": {"node_power_w": [3010.0, 3125.5], "gpu_power_w": {"0": [301.2]}}}

``covers`` matters: a type a source never reports must not be read as "missing" when comparing.
It is computed from what the source actually returned, never assumed.
"""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET

SCHEMA = "vcm-observed/1"

#: component types each source can observe; drift checks are limited to these
REDFISH_TYPES = ("cpu", "gpu", "dimm", "psu", "nic", "nvme", "disk")


def empty(source: str, covers: tuple[str, ...] = ()) -> dict:
    return {"schema": SCHEMA, "node": "", "sources": [source], "covers": sorted(covers),
            "components": [], "firmware": {}, "telemetry": {}}


def _clean(v) -> str:
    return str(v).strip() if v not in (None, "") else ""


def _num(text) -> float | None:
    m = re.search(r"-?\d+(\.\d+)?", str(text or ""))
    return float(m.group(0)) if m else None


# ---- Redfish ----------------------------------------------------------------------
def _odata_kind(body: dict) -> str:
    """``#Processor.v1_10_0.Processor`` -> ``Processor``."""
    t = str(body.get("@odata.type", "")).lstrip("#")
    return t.split(".", 1)[0] if t else ""


def _absent(body: dict) -> bool:
    return str((body.get("Status") or {}).get("State", "")).lower() == "absent"


def _slot(body: dict) -> str:
    loc = ((body.get("Location") or {}).get("PartLocation") or {})
    return _clean(loc.get("ServiceLabel") or body.get("Socket") or body.get("DeviceLocator")
                  or body.get("MemberId") or body.get("Id") or body.get("Name"))


def _component(kind: str, body: dict, **props) -> dict:
    return {"type": kind, "model": _clean(body.get("Model") or body.get("Name")),
            "mpn": _clean(body.get("PartNumber")), "serial": _clean(body.get("SerialNumber")),
            "slot": _slot(body), "props": {k: v for k, v in props.items() if v not in (None, "")}}


#: resource kinds ``parse_redfish`` reads; everything else is counted in the report, not dropped
#: silently
RECOGNISED_KINDS = ("ComputerSystem", "Manager", "Processor", "Memory", "PowerSupply", "Power",
                    "EnvironmentMetrics", "NetworkAdapter", "Drive", "SoftwareInventory")


def _norm(uri) -> str:
    return str(uri).rstrip("/")


def _links_fetched(body: dict, key: str, resources: dict) -> bool:
    """True if every member the resource lists was captured intact (an empty list counts)."""
    members = body.get(key)
    if not isinstance(members, list):
        return False
    for m in members:
        uri = _norm(m.get("@odata.id", "")) if isinstance(m, dict) else ""
        got = resources.get(uri)
        if not isinstance(got, dict) or "_error" in got:
            return False
    return True


def parse_redfish(capture: dict | str, report: dict | None = None) -> dict:
    """Parse a Redfish capture: ``{"resources": {uri: body}}`` or a bare ``{uri: body}`` map.

    ``covers`` lists only the component types this capture can actually vouch for: ones it
    contains, or whose whole collection it captured (even if empty). A BMC that does not expose
    memory must not make every declared DIMM look missing. Pass a dict as ``report`` to also get
    what was read, what was ignored and what could not be fetched (see ``vcm_parsers.diagnose``).
    """
    data = json.loads(capture) if isinstance(capture, str) else capture
    raw = data.get("resources", data) if isinstance(data, dict) else {}
    resources = {_norm(k): v for k, v in raw.items()}
    obs = empty("redfish")
    power_samples: list[float] = []
    rep = report if report is not None else {}
    rep.update(source="redfish", resources=len(resources), recognised={}, ignored={},
               fetch_errors=[], absent_skipped=0, systems=0, non_object=0,
               collections_complete=[], not_covered={})
    complete: set[str] = set()   # types proven covered by a fully captured collection

    for uri, body in sorted(resources.items()):
        if not isinstance(body, dict):
            rep["non_object"] += 1
            continue
        if "_error" in body:
            rep["fetch_errors"].append({"uri": uri, "error": str(body["_error"])[:200]})
            continue
        kind = _odata_kind(body)
        if kind in RECOGNISED_KINDS:
            rep["recognised"][kind] = rep["recognised"].get(kind, 0) + 1
        elif kind:
            ign = rep["ignored"].setdefault(kind, {"count": 0, "examples": []})
            ign["count"] += 1
            if len(ign["examples"]) < 3:
                ign["examples"].append(uri)
        if kind == "ComputerSystem":
            rep["systems"] += 1
            obs["node"] = obs["node"] or _clean(body.get("HostName") or body.get("Id"))
            if body.get("BiosVersion"):
                obs["firmware"]["bios"] = _clean(body["BiosVersion"])
        elif kind == "Manager" and body.get("FirmwareVersion"):
            obs["firmware"].setdefault("bmc", _clean(body["FirmwareVersion"]))
        elif kind == "ProcessorCollection" and _links_fetched(body, "Members", resources):
            complete.add("cpu")
        elif kind == "MemoryCollection" and _links_fetched(body, "Members", resources):
            complete.add("dimm")
        elif kind == "NetworkAdapterCollection" and _links_fetched(body, "Members", resources):
            complete.add("nic")
        elif kind == "PowerSupplyCollection" and _links_fetched(body, "Members", resources):
            complete.add("psu")
        elif kind == "Storage" and _links_fetched(body, "Drives", resources):
            complete.update(("nvme", "disk"))
        elif kind == "Processor":
            if _absent(body):
                rep["absent_skipped"] += 1
                continue
            ptype = str(body.get("ProcessorType", "CPU")).upper()
            obs["components"].append(_component(
                "cpu" if ptype == "CPU" else "gpu", body,
                cores=body.get("TotalCores"), max_speed_mhz=body.get("MaxSpeedMHz")))
        elif kind == "Memory":
            if _absent(body):
                rep["absent_skipped"] += 1
                continue
            mib = body.get("CapacityMiB")
            obs["components"].append(_component(
                "dimm", body, capacity_gb=round(mib / 1024) if mib else None,
                memory_type=_clean(body.get("MemoryDeviceType")),
                speed_mhz=body.get("OperatingSpeedMhz")))
        elif kind == "PowerSupply":
            if _absent(body):
                rep["absent_skipped"] += 1
                continue
            obs["components"].append(_component("psu", body, watts=body.get("PowerCapacityWatts")))
        elif kind == "Power":
            if isinstance(body.get("PowerSupplies"), list):
                complete.add("psu")   # the legacy Power resource lists every supply, even none
            for psu in body.get("PowerSupplies") or []:
                if _absent(psu):
                    rep["absent_skipped"] += 1
                else:
                    obs["components"].append(_component("psu", psu,
                                                         watts=psu.get("PowerCapacityWatts")))
            for ctl in body.get("PowerControl") or []:
                if ctl.get("PowerConsumedWatts") is not None:
                    power_samples.append(float(ctl["PowerConsumedWatts"]))
        elif kind == "EnvironmentMetrics":
            reading = (body.get("PowerWatts") or {}).get("Reading")
            if reading is not None:
                power_samples.append(float(reading))
        elif kind == "NetworkAdapter":
            if _absent(body):
                rep["absent_skipped"] += 1
                continue
            obs["components"].append(_component("nic", body))
        elif kind == "Drive":
            if _absent(body):
                rep["absent_skipped"] += 1
                continue
            proto = str(body.get("Protocol", "")).lower()
            cap = body.get("CapacityBytes")
            obs["components"].append(_component(
                "nvme" if proto == "nvme" else "disk", body,
                capacity_gb=round(cap / 1e9) if cap else None))
        elif kind == "SoftwareInventory" and body.get("Version"):
            name = _clean(body.get("Name") or body.get("Id"))
            obs["firmware"].setdefault(f"inventory:{name}", _clean(body["Version"]))
    if power_samples:
        obs["telemetry"]["node_power_w"] = power_samples
    seen = {c["type"] for c in obs["components"]}
    covers = seen | complete
    if covers & {"nvme", "disk"}:
        covers |= {"nvme", "disk"}   # drives are enumerated together: seeing any means all
    obs["covers"] = sorted(covers)
    why = {"cpu": "no Processor of type CPU and no complete ProcessorCollection",
           "gpu": "no Processor of type GPU: Redfish often does not list GPUs, use nvidia-smi",
           "dimm": "no Memory resources and no complete MemoryCollection",
           "psu": "no PowerSupply resources and no Power resource listing supplies",
           "nic": "no NetworkAdapter resources and no complete NetworkAdapterCollection",
           "nvme": "no NVMe Drive and no Storage resource with every drive captured",
           "disk": "no Drive and no Storage resource with every drive captured"}
    rep["not_covered"] = {t: w for t, w in why.items() if t not in covers}
    rep["collections_complete"] = sorted(complete)
    rep["components"] = {t: sum(c["type"] == t for c in obs["components"]) for t in sorted(seen)}
    return obs


# ---- nvidia-smi --------------------------------------------------------------------
def _csv_key(header: str) -> str:
    return re.sub(r"\s*\[.*?\]\s*", "", header).strip().lower()


#: nvidia-smi --query-gpu columns the parser reads
NVSMI_COLUMNS = ("index", "name", "serial", "pci.bus_id", "driver_version", "power.draw",
                 "power.limit", "pcie.link.gen.max", "pcie.link.width.max")


def _nvsmi_report(rep: dict, fmt: str) -> dict:
    rep.update(source="nvidia-smi", format=fmt, rows=0, columns=[], unread_columns=[],
               missing_columns=[], gpus=0, unnamed=0, not_covered={})
    return rep


def parse_nvidia_smi_csv(text: str, report: dict | None = None) -> dict:
    """``nvidia-smi --query-gpu=index,name,serial,pci.bus_id,driver_version,power.draw,...
    --format=csv`` output (with header). ``covers`` is ``gpu`` only if a GPU was listed."""
    obs = empty("nvidia-smi")
    rep = _nvsmi_report(report if report is not None else {}, "csv")
    rows = list(csv.reader(io.StringIO(text.strip()), skipinitialspace=True))
    if not rows:
        rep["not_covered"] = {"gpu": "nvidia-smi output was empty"}
        return obs
    keys = [_csv_key(h) for h in rows[0]]
    rep.update(rows=len(rows) - 1, columns=keys,
               unread_columns=[k for k in keys if k not in NVSMI_COLUMNS],
               missing_columns=[c for c in NVSMI_COLUMNS if c not in keys])
    gpu_power: dict[str, list[float]] = {}
    for row in rows[1:]:
        rec = dict(zip(keys, (c.strip() for c in row), strict=False))
        idx = rec.get("index", str(len(obs["components"])))
        if rec.get("driver_version"):
            obs["firmware"]["gpu_driver"] = rec["driver_version"]
        draw = _num(rec.get("power.draw"))
        if draw is not None:
            gpu_power.setdefault(idx, []).append(draw)
        # the same GPU may appear on several sampled rows; keep one component per index
        if any(c["props"].get("index") == idx for c in obs["components"]):
            continue
        obs["components"].append({
            "type": "gpu", "model": rec.get("name", ""), "mpn": "",
            "serial": rec.get("serial", ""), "slot": rec.get("pci.bus_id", f"GPU{idx}"),
            "props": {k: v for k, v in {
                "index": idx, "power_limit_w": _num(rec.get("power.limit")),
                "pcie_gen_max": _num(rec.get("pcie.link.gen.max")),
                "pcie_width_max": _num(rec.get("pcie.link.width.max"))}.items()
                if v not in (None, "")}})
    if gpu_power:
        obs["telemetry"]["gpu_power_w"] = gpu_power
    rep["gpus"] = len(obs["components"])
    rep["unnamed"] = sum(1 for c in obs["components"] if not c["model"])
    if obs["components"]:
        obs["covers"] = ["gpu"]
    else:
        rep["not_covered"] = {"gpu": "no GPU rows in the nvidia-smi output"}
    return obs


def _xt(el, path: str) -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_nvidia_smi_xml(text: str, report: dict | None = None) -> dict:
    """``nvidia-smi -q -x`` output."""
    obs = empty("nvidia-smi")
    rep = _nvsmi_report(report if report is not None else {}, "xml")
    root = ET.fromstring(text)
    if _xt(root, "driver_version"):
        obs["firmware"]["gpu_driver"] = _xt(root, "driver_version")
    if _xt(root, "cuda_version"):
        obs["firmware"]["cuda"] = _xt(root, "cuda_version")
    gpu_power: dict[str, list[float]] = {}
    for i, gpu in enumerate(root.findall("gpu")):
        idx = _xt(gpu, "minor_number") or str(i)
        draw = None
        for path in ("gpu_power_readings/power_draw", "power_readings/power_draw",
                     "gpu_power_readings/instant_power_draw"):
            draw = _num(_xt(gpu, path))
            if draw is not None:
                break
        if draw is not None:
            gpu_power[idx] = [draw]
        obs["components"].append({
            "type": "gpu", "model": _xt(gpu, "product_name"), "mpn": _xt(gpu, "board_part_number"),
            "serial": _xt(gpu, "serial"), "slot": gpu.get("id", f"GPU{idx}"),
            "props": {k: v for k, v in {
                "index": idx, "vbios": _xt(gpu, "vbios_version"),
                "pcie_gen_max": _num(_xt(gpu, "pci/pci_gpu_link_info/pcie_gen/max_link_gen")),
                "pcie_gen_current": _num(_xt(gpu, "pci/pci_gpu_link_info/pcie_gen/current_link_gen")),
                "pcie_width_max": _num(_xt(gpu, "pci/pci_gpu_link_info/link_widths/max_link_width")),
                }.items() if v not in (None, "")}})
    if gpu_power:
        obs["telemetry"]["gpu_power_w"] = gpu_power
    rep["gpus"] = len(obs["components"])
    rep["rows"] = rep["gpus"]
    rep["unnamed"] = sum(1 for c in obs["components"] if not c["model"])
    if obs["components"]:
        obs["covers"] = ["gpu"]
    else:
        rep["not_covered"] = {"gpu": "no <gpu> elements in the nvidia-smi output"}
    return obs


def parse_nvidia_smi(text: str, report: dict | None = None) -> dict:
    if text.lstrip().startswith("<"):
        return parse_nvidia_smi_xml(text, report)
    return parse_nvidia_smi_csv(text, report)


# ---- combining ---------------------------------------------------------------------
def merge(observations: list[dict]) -> dict:
    """Combine observations of one node. A later source replaces earlier components of every type
    it covers (nvidia-smi names GPUs more precisely than most BMCs), and adds its firmware and
    telemetry."""
    out = empty("merged")
    out["sources"] = []
    for obs in observations:
        covers = set(obs.get("covers", []))
        out["components"] = [c for c in out["components"] if c["type"] not in covers]
        out["components"] += obs.get("components", [])
        out["firmware"].update(obs.get("firmware", {}))
        for k, v in (obs.get("telemetry") or {}).items():
            out["telemetry"][k] = v
        out["covers"] = sorted(set(out["covers"]) | covers)
        out["sources"] += [s for s in obs.get("sources", []) if s not in out["sources"]]
        out["node"] = out["node"] or obs.get("node", "")
    return out


def to_node_manifest(obs: dict) -> dict:
    """Draft a ``vcm-node/1`` manifest from an observation.

    Only what was observed is filled in. Connections (power domains, PCIe roots), chassis cooling
    and redundancy policy are not visible to these sources and are left for a human to declare.
    """
    counters: dict[str, int] = {}
    comps = []
    for c in sorted(obs.get("components", []), key=lambda c: (c["type"], c["slot"])):
        n = counters.get(c["type"], 0)
        counters[c["type"]] = n + 1
        entry = {"ref": f"{c['type'].upper()}{n}", "type": c["type"], "model": c["model"]}
        for k in ("mpn", "serial", "slot"):
            if c.get(k):
                entry[k] = c[k]
        entry.update({k: v for k, v in (c.get("props") or {}).items() if k not in ("index",)})
        entry["connections"] = {}
        comps.append(entry)
    firmware = {k: v for k, v in obs.get("firmware", {}).items() if not k.startswith("inventory:")}
    return {"schema": "vcm-node/1", "node": obs.get("node") or "unnamed-node",
            "observed_from": obs.get("sources", []), "firmware": firmware,
            "power": {"domains": {}}, "components": comps}
