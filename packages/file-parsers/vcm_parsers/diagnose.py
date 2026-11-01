"""The inventory doctor: what a capture gave VCM, and, more importantly, what it did not.

A parser that quietly skips what it does not understand makes a gap look like a clean result. The
doctor says out loud which resource kinds were present but not read, which could not be fetched,
which component types the capture cannot vouch for (so drift will not judge them), and which
components are too thinly described for drift to match.

Findings are split into *problems* (the capture is incomplete or unusable for drift: fix or
re-capture) and *warnings* (VCM works, but not on everything the machine reported).
"""
from __future__ import annotations

import json

from .inventory import merge, parse_nvidia_smi, parse_redfish

#: Redfish kinds that are scaffolding, so ignoring them loses nothing the comparison needs
STRUCTURAL = {
    "ServiceRoot", "Chassis", "Storage", "StorageController", "Volume", "LogService", "LogEntry",
    "Session", "SessionService", "AccountService", "ManagerAccount", "Role", "EventService",
    "EventDestination", "TaskService", "Task", "UpdateService", "TelemetryService",
    "CertificateService", "Certificate", "ManagerNetworkProtocol", "EthernetInterface",
    "VirtualMedia", "SecureBoot", "MessageRegistryFile", "JsonSchemaFile", "Message",
    "ProcessorMetrics", "MemoryMetrics", "HostInterface", "SerialInterface", "Triggers",
    "ComponentIntegrity", "LicenseService", "Settings", "Protocol",
}
#: kinds that describe hardware or firmware this release does not read
LIKELY_RELEVANT = {
    "PCIeDevice", "PCIeFunction", "Assembly", "Thermal", "ThermalSubsystem", "PowerSubsystem",
    "Sensor", "Fan", "NetworkDeviceFunction", "NetworkInterface", "Bios", "Port", "Switch",
    "Fabric", "Endpoint", "TrustedComponent", "Battery", "PowerSupplyMetrics", "Drive",
    "Memory", "Processor", "PowerSupply",
}


def _kind_class(kind: str) -> str:
    if kind.endswith("Collection") or kind in STRUCTURAL:
        return "structural"
    return "relevant" if kind in LIKELY_RELEVANT else "unrecognised"


def doctor(redfish: str | dict | None = None, nvidia_smi: str | None = None) -> dict:
    """Diagnose captures. Pass Redfish JSON (text or object) and/or nvidia-smi output (text)."""
    if redfish is None and nvidia_smi is None:
        raise ValueError("give a Redfish capture, nvidia-smi output, or both")
    problems: list[str] = []
    warnings: list[str] = []
    reports: dict[str, dict] = {}
    observations = []

    if redfish is not None:
        rep: dict = {}
        try:
            obs = parse_redfish(redfish, rep)
        except (ValueError, AttributeError, TypeError) as exc:
            problems.append(f"the Redfish capture could not be parsed: {exc}")
        else:
            observations.append(obs)
            reports["redfish"] = rep
            if rep["fetch_errors"]:
                ex = "; ".join(f"{e['uri']} ({e['error'][:60]})" for e in rep["fetch_errors"][:3])
                problems.append(f"{len(rep['fetch_errors'])} Redfish resource(s) could not be "
                                f"fetched, so the capture is incomplete: {ex}")
            if rep["systems"] > 1:
                warnings.append(f"the capture holds {rep['systems']} computer systems; their "
                                "components are merged, so drift against a single node "
                                "manifest will be wrong. Capture one node at a time.")
            if rep["systems"] == 0:
                warnings.append("no ComputerSystem resource: the node name and BIOS version "
                                "are unknown")
            if not obs["components"]:
                problems.append("the Redfish capture yielded no components at all; it is "
                                "probably not a full walk of the service")
            groups: dict[str, list[str]] = {"relevant": [], "unrecognised": []}
            for kind, info in sorted(rep["ignored"].items()):
                cls = _kind_class(kind)
                if cls != "structural":
                    groups[cls].append(f"{kind} x{info['count']} (e.g. {info['examples'][0]})")
            if groups["relevant"]:
                warnings.append("present in the capture but not read by VCM: "
                                + ", ".join(groups["relevant"][:8]))
            if groups["unrecognised"]:
                warnings.append("resource kinds VCM does not know: "
                                + ", ".join(groups["unrecognised"][:8])
                                + ". If any describe parts you care about, share the capture "
                                  "(scrubbed) so the parser can learn them.")
            if rep["not_covered"]:
                warnings.append(
                    "Redfish cannot vouch for: " + ", ".join(sorted(rep["not_covered"]))
                    + ", so drift will not judge those types from it ("
                    + "; ".join(f"{t}: {w}" for t, w in sorted(rep["not_covered"].items())[:3])
                    + (" ..." if len(rep["not_covered"]) > 3 else "") + ")")
            if "bios" not in obs["firmware"]:
                warnings.append("no BIOS version found (ComputerSystem.BiosVersion)")
            if "bmc" not in obs["firmware"]:
                warnings.append("no BMC firmware version found (Manager.FirmwareVersion)")
            if "node_power_w" not in obs["telemetry"]:
                warnings.append("no power reading (Power.PowerControl or EnvironmentMetrics): "
                                "measured draw will be unavailable")

    if nvidia_smi is not None:
        rep = {}
        try:
            obs = parse_nvidia_smi(nvidia_smi, rep)
        except (ValueError, TypeError) as exc:
            problems.append(f"the nvidia-smi output could not be parsed: {exc}")
        else:
            observations.append(obs)
            reports["nvidia_smi"] = rep
            if not obs["components"]:
                problems.append("no GPUs found in the nvidia-smi output")
            if rep["format"] == "csv" and rep["columns"] and "name" not in rep["columns"]:
                problems.append("the nvidia-smi CSV has no 'name' column; if there is no header "
                                "row, add --format=csv (not csv,noheader)")
            elif rep["missing_columns"] and rep["format"] == "csv":
                warnings.append("nvidia-smi columns not in the query, so not used: "
                                + ", ".join(rep["missing_columns"]))
            if rep.get("null_columns"):
                warnings.append("the machine reported no value for: "
                                + ", ".join(rep["null_columns"])
                                + ". Checks that read those fields did not run; this is a gap "
                                  "in what the machine told VCM, not a clean result.")
            if rep["unread_columns"]:
                warnings.append("nvidia-smi columns VCM does not read: "
                                + ", ".join(rep["unread_columns"]))
            if rep["unnamed"]:
                problems.append(f"{rep['unnamed']} GPU(s) have no model name; drift cannot "
                                "match them")
            if "gpu_driver" not in obs["firmware"]:
                warnings.append("no driver version in the nvidia-smi output: the driver "
                                "minimum check cannot compare against the machine")

    merged = merge(observations) if observations else None
    unidentifiable = []
    if merged:
        for c in merged["components"]:
            if not (c["model"] or c["mpn"]):
                unidentifiable.append(f"{c['type']} in {c['slot'] or 'unknown slot'}")
        if unidentifiable:
            problems.append(f"{len(unidentifiable)} component(s) report neither a model nor a "
                            f"part number, so drift cannot match them: "
                            + ", ".join(unidentifiable[:6]))
    return {"schema": "vcm-doctor/1", "ok": not problems, "problems": problems,
            "warnings": warnings, "reports": reports,
            "components": {t: sum(c["type"] == t for c in merged["components"])
                           for t in sorted({c["type"] for c in merged["components"]})}
            if merged else {},
            "covers": merged["covers"] if merged else [],
            "firmware": sorted(merged["firmware"]) if merged else [],
            "telemetry": sorted(merged["telemetry"]) if merged else []}


def render_doctor(res: dict) -> str:
    lines = [f"inventory doctor: {'OK' if res['ok'] else 'PROBLEMS FOUND'}"]
    comp = ", ".join(f"{t} {n}" for t, n in res["components"].items()) or "none"
    lines.append(f"  read: {comp}")
    lines.append(f"  can vouch for (drift will judge): {', '.join(res['covers']) or 'nothing'}")
    lines.append(f"  firmware found: {', '.join(res['firmware']) or 'none'}; "
                 f"telemetry: {', '.join(res['telemetry']) or 'none'}")
    for p in res["problems"]:
        lines.append(f"  PROBLEM  {p}")
    for w in res["warnings"]:
        lines.append(f"  warning  {w}")
    if res["ok"] and not res["warnings"]:
        lines.append("  nothing skipped or missing")
    return "\n".join(lines)


def load_capture(path: str) -> str:
    """Read a capture file as text, unwrapping nothing: parsers accept the text as written."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def dumps(res: dict) -> str:
    return json.dumps(res, indent=2, sort_keys=True)
