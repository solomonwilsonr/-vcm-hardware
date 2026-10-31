import json
import shutil
from pathlib import Path

import pytest

from vcm_ai import analyze
from vcm_cli.main import main
from vcm_core import Repository
from vcm_diff import compare_drift, models_match
from vcm_parsers import (
    build_design,
    merge_observations,
    parse_nvidia_smi,
    parse_redfish,
    to_node_manifest,
)

ROOT = Path(__file__).resolve().parent.parent
NODE = ROOT / "examples" / "gpu-node"
OBS = NODE / "observed"

NVSMI_XML = """<?xml version="1.0" ?>
<nvidia_smi_log>
  <driver_version>535.129.03</driver_version>
  <cuda_version>12.2</cuda_version>
  <gpu id="00000000:17:00.0">
    <product_name>NVIDIA H100 NVL</product_name>
    <serial>1650123456789</serial>
    <minor_number>0</minor_number>
    <board_part_number>900-21010-0000-000</board_part_number>
    <pci><pci_gpu_link_info><pcie_gen><max_link_gen>5</max_link_gen>
      <current_link_gen>4</current_link_gen></pcie_gen>
      <link_widths><max_link_width>16x</max_link_width></link_widths></pci_gpu_link_info></pci>
    <gpu_power_readings><power_draw>312.40 W</power_draw></gpu_power_readings>
  </gpu>
</nvidia_smi_log>
"""


def _design(version="v1.0"):
    text = (NODE / version / "gpu-node-01.node.json").read_bytes()
    d = build_design({"gpu-node-01.node.json": text})
    return d, d.meta["nodes"][0]


def _observed():
    return merge_observations([parse_redfish((OBS / "redfish-capture.json").read_text()),
                               parse_nvidia_smi((OBS / "nvidia-smi.csv").read_text())])


def test_parse_redfish_capture():
    obs = parse_redfish((OBS / "redfish-capture.json").read_text())
    by_type = {}
    for c in obs["components"]:
        by_type[c["type"]] = by_type.get(c["type"], 0) + 1
    assert by_type == {"cpu": 2, "gpu": 8, "dimm": 15, "psu": 4, "nic": 2, "nvme": 2}
    assert obs["node"] == "gpu-node-01"
    assert obs["firmware"]["bios"] == "3.4" and obs["firmware"]["bmc"] == "1.12.03"
    assert sorted(obs["telemetry"]["node_power_w"]) == [2710.0, 2865.0]
    dimm = next(c for c in obs["components"] if c["type"] == "dimm")
    assert dimm["mpn"] == "M393A4K40DB3-CWE" and dimm["props"]["capacity_gb"] == 32


def test_parse_nvidia_smi_csv_and_xml():
    csv_obs = parse_nvidia_smi((OBS / "nvidia-smi.csv").read_text())
    assert len(csv_obs["components"]) == 8
    assert csv_obs["firmware"] == {"gpu_driver": "470.199.02"}
    assert csv_obs["telemetry"]["gpu_power_w"]["0"] == [231.4]
    assert csv_obs["components"][0]["props"]["power_limit_w"] == 250.0

    xml_obs = parse_nvidia_smi(NVSMI_XML)
    g = xml_obs["components"][0]
    assert g["model"] == "NVIDIA H100 NVL" and g["mpn"] == "900-21010-0000-000"
    assert g["props"]["pcie_gen_max"] == 5 and g["props"]["pcie_gen_current"] == 4
    assert xml_obs["firmware"] == {"gpu_driver": "535.129.03", "cuda": "12.2"}
    assert xml_obs["telemetry"]["gpu_power_w"] == {"0": [312.4]}


def test_merge_later_source_replaces_covered_types():
    obs = _observed()
    gpus = [c for c in obs["components"] if c["type"] == "gpu"]
    assert len(gpus) == 8 and all(g["slot"].startswith("00000000:") for g in gpus)
    assert obs["sources"] == ["redfish", "nvidia-smi"]
    assert obs["firmware"]["gpu_driver"] == "470.199.02"


@pytest.mark.parametrize("declared,observed,same", [
    ({"model": "Intel Xeon Gold 6338"}, {"model": "Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz"}, True),
    ({"model": "NVIDIA A100 PCIe 40GB"}, {"model": "NVIDIA A100-PCIE-40GB"}, True),
    ({"model": "NVIDIA A100 PCIe 40GB"}, {"model": "NVIDIA H100 NVL"}, False),
    ({"model": "x", "mpn": "MZQL23T8HCLS"}, {"model": "y", "mpn": "MZQL23T8HCLS-00A07"}, True),
    ({"model": ""}, {"model": "anything"}, False),
])
def test_models_match(declared, observed, same):
    assert models_match(declared, observed) is same


def test_drift_finds_failed_dimm_and_older_driver():
    design, meta = _design("v1.0")
    res = compare_drift(design, meta, _observed())
    assert res["status"] == "drifted"
    kinds = {(f["kind"], tuple(f["refs"])) for f in res["findings"]}
    assert ("missing", ("DIMM11",)) in kinds          # attributed to the slot that is empty
    drv = next(f for f in res["findings"] if f["kind"] == "firmware")
    assert drv["severity"] == "critical" and "OLDER" in drv["detail"]
    assert all(f["confidence_class"] == "verified" for f in res["findings"])


def test_drift_only_checks_types_the_source_can_see():
    design, meta = _design("v1.0")
    gpu_only = parse_nvidia_smi((OBS / "nvidia-smi.csv").read_text())
    res = compare_drift(design, meta, gpu_only)
    assert res["checked_types"] == ["gpu"]
    assert {"cpu", "dimm", "nic", "psu"} <= set(res["unchecked_types"])
    assert not any(f["kind"] == "missing" for f in res["findings"])


def test_drift_detects_wrong_part_fitted():
    design, meta = _design("v1.1")  # manifest says H100 NVL; the machine still has A100s
    res = compare_drift(design, meta, _observed())
    mism = [f for f in res["findings"] if f["kind"] == "model_mismatch"]
    assert len(mism) == 8 and "A100" in mism[0]["title"]


def test_in_sync_when_machine_matches():
    design, meta = _design("v1.0")
    obs = _observed()
    obs["firmware"]["gpu_driver"] = meta["firmware"]["gpu_driver"]
    obs["components"].append({"type": "dimm", "model": "", "mpn": "M393A4K40DB3-CWE",
                              "serial": "", "slot": "DIMM_B3", "props": {}})
    res = compare_drift(design, meta, obs)
    assert res["status"] == "in_sync", res["findings"]


def test_manifest_draft_leaves_unobservable_fields_empty():
    draft = to_node_manifest(_observed())
    assert draft["schema"] == "vcm-node/1" and draft["power"]["domains"] == {}
    assert all(c["connections"] == {} for c in draft["components"])
    assert sum(c["type"] == "gpu" for c in draft["components"]) == 8


@pytest.fixture
def node_repo(tmp_path):
    r = Repository.init(tmp_path / "node")
    for v in ("v1.0", "v1.1"):
        for p in list(r.root.iterdir()):
            if p.name != ".vcm":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(NODE / v, r.root, dirs_exist_ok=True)
        r.commit(v, author="t", timestamp="2026-01-01T00:00:00Z")
        r.tag(v)
    return r


def test_impact_cites_observed_base(node_repo, capsys):
    before = analyze(node_repo, "v1.0", "v1.1")
    assert not any("machine" in f["title"] for f in before["findings"])
    code = main(["-C", str(node_repo.root), "drift", "--rev", "v1.0", "--attach",
                 "--redfish", str(OBS / "redfish-capture.json"),
                 "--nvidia-smi", str(OBS / "nvidia-smi.csv")])
    assert code == 1  # drifted
    rep = analyze(node_repo, "v1.0", "v1.1")
    by_title = {f["title"]: f for f in rep["findings"]}
    mismatch = by_title["The base configuration does not match the machine it describes"]
    assert mismatch["confidence_class"] == "verified" and mismatch["severity"] == "critical"
    measured = next(f for f in rep["findings"] if f["title"].startswith("Measured base draw"))
    assert measured["confidence_class"] == "verified" and "2865 W" in measured["title"]
    proj = next(f for f in rep["findings"] if f["title"].startswith("Projected draw"))
    assert proj["confidence_class"] == "speculative" and proj["derived_from"] == [measured["id"]]
    assert node_repo.audit.events()[-1]["action"] == "observe"


def test_cli_inventory_import_and_manifest(tmp_path, capsys):
    out = tmp_path / "obs.json"
    assert main(["inventory", "import", "--redfish", str(OBS / "redfish-capture.json"),
                 "--out", str(out)]) == 0
    assert json.loads(out.read_text())["schema"] == "vcm-observed/1"
    draft = tmp_path / "node.json"
    assert main(["inventory", "manifest", "--observed", str(out), "--out", str(draft)]) == 0
    assert json.loads(draft.read_text())["node"] == "gpu-node-01"
