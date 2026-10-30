import copy
import json
from pathlib import Path

import pytest

from vcm_diff import compare_drift
from vcm_parsers import (
    build_design,
    doctor,
    merge_observations,
    parse_nvidia_smi,
    parse_redfish,
    render_doctor,
)
from vcm_parsers.sanitize import Scrubber, sanitize_bundle

ROOT = Path(__file__).resolve().parent.parent
OBS = ROOT / "examples" / "gpu-node" / "observed"
REDFISH = json.loads((OBS / "redfish-capture.json").read_text())
CSV = (OBS / "nvidia-smi.csv").read_text()


def _dirty():
    """The sample capture with the kinds of things a real BMC adds."""
    cap = copy.deepcopy(REDFISH)
    res = cap["resources"]
    res["/redfish/v1/Systems/1"].update({
        "HostName": "gpu-prod-17.corp.example.com", "UUID": "4c4c4544-0042-3010-8051-b8c04f435a31",
        "BiosVersion": "2.14.0.0", "Description": "owned by jane.doe@corp.example.com, rack 9"})
    res["/redfish/v1/Managers/1/EthernetInterfaces/1"] = {
        "@odata.type": "#EthernetInterface.v1_6_0.EthernetInterface", "Id": "1",
        "MACAddress": "AA:BB:CC:11:22:33",
        "IPv4Addresses": [{"Address": "192.168.5.7", "Gateway": "192.168.5.1",
                           "SubnetMask": "255.255.255.0"}], "HostName": "gpu-prod-17"}
    res["/redfish/v1/AccountService/Accounts/2"] = {
        "@odata.type": "#ManagerAccount.v1_8_0.ManagerAccount", "Id": "2",
        "UserName": "admin", "Password": "hunter2-super-secret"}
    res["/redfish/v1/Managers/1/NetworkProtocol"] = {
        "@odata.type": "#ManagerNetworkProtocol.v1_5_0.ManagerNetworkProtocol",
        "NTP": {"NTPServers": ["time.corp.example.com"]},
        "Oem": {"LogUri": "https://svc:s3cr3t@10.0.0.9/logs"},
        "Note": "managed by gpu-prod-17 since the 2.14.0.0 firmware"}
    return cap


def _shape(obs):
    return {"components": sorted((c["type"], c["model"], c["mpn"], c["slot"])
                                 for c in obs["components"]),
            "firmware": obs["firmware"], "covers": obs["covers"],
            "telemetry": obs["telemetry"]}


# ---- sanitizer ---------------------------------------------------------------------
def test_identifiers_secrets_and_addresses_are_replaced():
    raw = json.dumps(_dirty())
    out = sanitize_bundle({"redfish.json": raw}, salt="s")
    text = out["files"]["redfish.json"]
    for secret in ("gpu-prod-17", "corp.example.com", "jane.doe", "4c4c4544-0042", "AA:BB:CC:11",
                   "192.168.5.7", "192.168.5.1", "hunter2", "s3cr3t", "10.0.0.9",
                   "14212210", "P0000", "S00001"):
        assert secret not in text, secret
    assert out["leaks"]["residual"] == []
    s = out["summary"]
    assert s["identifier"] >= 10 and s["ip"] >= 3 and s["secret"] >= 2 and s["mac"] >= 1
    assert "[REDACTED]" in text


def test_what_the_parser_needs_is_left_exactly_alone():
    text = sanitize_bundle({"r.json": json.dumps(_dirty())}, salt="s")["files"]["r.json"]
    cap = json.loads(text)
    assert cap["resources"]["/redfish/v1/Systems/1"]["BiosVersion"] == "2.14.0.0"  # a "quad"
    assert "NVIDIA A100-PCIE-40GB" in text and "M393A4K40DB3-CWE" in text
    assert "255.255.255.0" in text                       # a netmask is not identifying
    assert "since the 2.14.0.0 firmware" in text          # bare dotted numbers in prose stay
    res = cap["resources"]["/redfish/v1/Managers/1/EthernetInterfaces/1"]
    assert res["IPv4Addresses"][0]["Address"].startswith("10.")


def test_a_scrubbed_capture_parses_the_same_as_the_original():
    raw = json.dumps(_dirty())
    clean = sanitize_bundle({"r.json": raw}, salt="s")["files"]["r.json"]
    a, b = parse_redfish(raw), parse_redfish(clean)
    assert _shape(a) == _shape(b)
    assert a["node"] != b["node"] and "gpu-prod-17" not in b["node"]
    assert [c["serial"] for c in a["components"]] != [c["serial"] for c in b["components"]]
    da, db = doctor(raw, CSV), doctor(clean, CSV)
    assert (da["problems"], da["warnings"], da["components"], da["covers"]) == (
        db["problems"], db["warnings"], db["components"], db["covers"])
    # and drift against a manifest reaches the same verdict on both
    node = (ROOT / "examples" / "gpu-node" / "v1.0" / "gpu-node-01.node.json").read_bytes()
    design = build_design({"n.node.json": node})
    meta = design.meta["nodes"][0]
    ra = compare_drift(design, meta, merge_observations([a, parse_nvidia_smi(CSV)]))
    rb = compare_drift(design, meta, merge_observations([b, parse_nvidia_smi(CSV)]))
    assert [(f["kind"], f["refs"]) for f in ra["findings"]] == [(f["kind"], f["refs"])
                                                                for f in rb["findings"]]


def test_identifiers_correlate_across_files():
    out = sanitize_bundle({"redfish.json": json.dumps(REDFISH), "gpus.csv": CSV}, salt="s")
    r = parse_redfish(out["files"]["redfish.json"])
    n = parse_nvidia_smi(out["files"]["gpus.csv"])
    rs = {c["serial"] for c in r["components"] if c["type"] == "gpu"}
    ns = {c["serial"] for c in n["components"]}
    assert rs == ns and len(ns) == 8                      # same stand-in in both files
    assert not rs & {c["serial"] for c in parse_nvidia_smi(CSV)["components"]}


def test_csv_xml_and_text_are_scrubbed_too():
    xml = ('<nvidia_smi_log><driver_version>535.129.03</driver_version><gpu id="00:17.0">'
           "<product_name>NVIDIA H100 NVL</product_name><serial>1650123456789</serial>"
           "<uuid>GPU-4c4c4544-0042-3010-8051-b8c04f435a31</uuid></gpu></nvidia_smi_log>")
    out = sanitize_bundle({"g.xml": xml, "g.csv": CSV}, salt="s")["files"]
    assert "1650123456789" not in out["g.xml"] and "535.129.03" in out["g.xml"]
    assert "NVIDIA H100 NVL" in out["g.xml"]
    assert "142122100000" not in out["g.csv"] and "470.199.02" in out["g.csv"]
    assert parse_nvidia_smi(out["g.xml"])["components"][0]["model"] == "NVIDIA H100 NVL"


def test_pseudonyms_are_stable_with_a_salt_and_unlinkable_without():
    raw = json.dumps(_dirty())
    a = sanitize_bundle({"r": raw}, salt="same")["files"]["r"]
    assert a == sanitize_bundle({"r": raw}, salt="same")["files"]["r"]
    assert a != sanitize_bundle({"r": raw}, salt="other")["files"]["r"]
    assert sanitize_bundle({"r": raw})["files"]["r"] != sanitize_bundle({"r": raw})["files"]["r"]


def test_pseudonyms_keep_the_shape_of_what_they_replace():
    sc = Scrubber("s")
    out = sc.shaped("1421221012345", "serial")
    assert len(out) == 13 and out.isdigit() and out != "1421221012345"
    out = sc.shaped("SN-AB12cd", "serial")
    assert out[2] == "-" and out[:2].isupper() and out[3:5].isupper() and out[5:7].isdigit()
    mac = sc.shaped("AA:BB:CC:11:22:33", "mac", hexlike=True)
    assert len(mac) == 17 and mac.count(":") == 5 and set(mac.replace(":", "")) <= set(
        "0123456789ABCDEF")


def test_extra_keys_and_leak_report():
    data = {"resources": {"/x": {"@odata.type": "#Thing.v1.Thing", "Owner": "Acme Datacenter 4",
                                 "Contact": "see 172.16.9.200 for details"}}}
    out = sanitize_bundle({"c.json": json.dumps(data)}, salt="s", extra_keys=("Owner",))
    assert "Acme Datacenter 4" not in out["files"]["c.json"]
    # a bare address in prose is not auto-scrubbed (could be a version); it is reported instead
    assert any("172.16.9.200" in p for p in out["leaks"]["possible"])
    assert "172.16.9.200" in out["files"]["c.json"]


def test_leak_check_flags_a_failure_to_scrub():
    sc = Scrubber("s")
    sc.learn("SERIAL-12345", "identifier")
    assert sc.leaks({"f": "ok SERIAL-12345 leaked"})["residual"]
    assert not sc.leaks({"f": "nothing here"})["residual"]


# ---- doctor ------------------------------------------------------------------------
def test_doctor_is_clean_on_the_sample():
    res = doctor((OBS / "redfish-capture.json").read_text(), CSV)
    assert res["ok"] and res["problems"] == []
    assert res["components"] == {"cpu": 2, "dimm": 15, "gpu": 8, "nic": 2, "nvme": 2, "psu": 4}
    assert set(res["covers"]) >= {"cpu", "dimm", "gpu", "nic", "nvme", "psu"}
    assert "doctor: OK" in render_doctor(res)


def test_doctor_reports_fetch_errors_and_unread_kinds():
    cap = copy.deepcopy(REDFISH)
    cap["resources"]["/redfish/v1/Systems/1/Memory"] = {"_error": "HTTP 500"}
    cap["resources"]["/redfish/v1/Chassis/1/PCIeDevices/0"] = {
        "@odata.type": "#PCIeDevice.v1_9_0.PCIeDevice", "Id": "0"}
    cap["resources"]["/redfish/v1/Oem/Vendor"] = {"@odata.type": "#VendorThing.v1_0_0.VendorThing"}
    res = doctor(json.dumps(cap))
    assert not res["ok"] and "could not be fetched" in res["problems"][0]
    joined = " ".join(res["warnings"])
    assert "PCIeDevice x1" in joined and "VendorThing x1" in joined
    assert "share the capture" in joined


def test_a_bmc_without_memory_does_not_make_every_dimm_look_missing():
    cap = copy.deepcopy(REDFISH)
    cap["resources"] = {k: v for k, v in cap["resources"].items() if "/Memory/" not in k}
    obs = parse_redfish(cap)
    assert "dimm" not in obs["covers"]
    node = (ROOT / "examples" / "gpu-node" / "v1.0" / "gpu-node-01.node.json").read_bytes()
    design = build_design({"n.node.json": node})
    res = compare_drift(design, design.meta["nodes"][0], obs)
    assert not any(f["kind"] == "missing" and f["type"] == "dimm" for f in res["findings"])
    assert "dimm" in res["unchecked_types"]


def test_an_explicitly_empty_collection_is_a_real_answer():
    cap = {"resources": {"/redfish/v1/Systems/1/Memory": {
        "@odata.type": "#MemoryCollection.MemoryCollection", "Members": []}}}
    assert "dimm" in parse_redfish(cap)["covers"]
    cap["resources"]["/redfish/v1/Systems/1/Memory"]["Members"] = [
        {"@odata.id": "/redfish/v1/Systems/1/Memory/DIMM0"}]
    assert "dimm" not in parse_redfish(cap)["covers"]     # a listed member we never captured


def test_redfish_without_gpus_does_not_claim_them(tmp_path):
    cap = copy.deepcopy(REDFISH)
    cap["resources"] = {k: v for k, v in cap["resources"].items() if "/GPU" not in k}
    obs = parse_redfish(cap)
    assert "gpu" not in obs["covers"] and "cpu" in obs["covers"]
    assert any("gpu" in w for w in doctor(json.dumps(cap))["warnings"])


def test_doctor_on_thin_or_wrong_inputs():
    assert any("no GPUs" in p for p in doctor(None, "index, name\n")["problems"])
    headerless = "0, NVIDIA A100, 1234567, 00:17.0, 470.1, 200 W\n"
    assert any("no 'name' column" in p for p in doctor(None, headerless)["problems"])
    unnamed = "index, name, serial\n0, , 123456\n"
    assert any("no model name" in p for p in doctor(None, unnamed)["problems"])
    two = copy.deepcopy(REDFISH)
    two["resources"]["/redfish/v1/Systems/2"] = {
        "@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem", "Id": "2"}
    assert any("2 computer systems" in w for w in doctor(json.dumps(two))["warnings"])
    assert not doctor("{}")["ok"]
    with pytest.raises(ValueError):
        doctor()
