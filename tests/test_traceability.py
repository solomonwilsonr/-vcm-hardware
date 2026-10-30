import pytest

from vcm_core import VcmError
from vcm_trace import (
    affected_by_component,
    affected_by_revision,
    list_devices,
    passport,
    register_device,
)
from vcm_workflow import create_release


@pytest.fixture
def fleet(sample_repo):
    create_release(sample_repo, "R1.0", "v1.0")
    create_release(sample_repo, "R1.1", "v1.1")
    register_device(sample_repo, "SN-0001", "R1.0", batch="B1")
    register_device(sample_repo, "SN-0002", "R1.0", batch="B1")
    register_device(sample_repo, "SN-0003", "R1.1", batch="B2")
    return sample_repo


def test_register_creates_record_and_qr(fleet):
    dev = [d for d in list_devices(fleet) if d["serial"] == "SN-0001"][0]
    assert dev["qr_payload"].endswith("/SN-0001") and dev["revision"] == fleet.resolve("v1.0")
    assert (fleet.dir / "devices" / "SN-0001.svg").read_text().startswith("<svg")


def test_register_validation(fleet):
    with pytest.raises(VcmError):
        register_device(fleet, "SN-0001", "R1.0")  # duplicate
    with pytest.raises(VcmError):
        register_device(fleet, "SN-9", "nope")  # unknown release
    with pytest.raises(VcmError):
        register_device(fleet, "bad serial!", "R1.0")


def test_passport_lineage(fleet):
    p = passport(fleet, "SN-0003")
    assert p["release"]["name"] == "R1.1" and p["revision"]["tags"] == ["v1.1"]
    assert [x["message"] for x in p["lineage"]] == [
        "v1.0 initial", "v1.1 swap regulator, change output cap"]
    assert {c["ref"]: c["value"] for c in p["bom"]}["U1"] == "MCP1700-3302E/TT"
    assert p["validation"]["status"] == "pass"
    assert p["qr"]["svg"].startswith("<svg") and p["release"]["manifest_sha256"]
    assert any(f["path"] == "firmware/main.c" for f in p["firmware"])
    p1 = passport(fleet, "SN-0001")
    assert [x["message"] for x in p1["lineage"]] == ["v1.0 initial"]


def test_affected_by_component_value_filter(fleet):
    allu1 = affected_by_component(fleet, "U1")
    assert {d["serial"] for d in allu1} == {"SN-0001", "SN-0002", "SN-0003"}
    old = affected_by_component(fleet, "U1", value="AMS1117-3.3")
    assert {d["serial"] for d in old} == {"SN-0001", "SN-0002"}
    assert affected_by_component(fleet, "U99") == []


def test_affected_by_revision_follows_lineage(fleet):
    res = affected_by_revision(fleet, "v1.1")
    assert res["changed_components"] == ["C2", "R1", "U1"]
    assert [d["serial"] for d in res["devices"]] == ["SN-0003"]
    res0 = affected_by_revision(fleet, "v1.0")
    assert len(res0["devices"]) == 3 and "U1" in res0["changed_components"]
