"""Datacenter node manifests: parsing, and the power/PCIe/driver rules built on them.

The claim under test is that the engine is domain-agnostic. The same object store, diff, graph,
confidence classes and evidence model that handle a circuit board handle a GPU chassis, with only
the input format, the catalog and the rules changing.
"""
import json
import shutil
from pathlib import Path

import pytest

from vcm_ai import analyze
from vcm_ai.rules_datacenter import _version, node_meta, usable_watts
from vcm_core import Repository
from vcm_parsers import design_from_revision, looks_like_node, parse_node

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "gpu-node"


@pytest.fixture
def node_repo(tmp_path):
    """Repository with the sample GPU node committed as v1.0 (A100) and v1.1 (H100 NVL)."""
    r = Repository.init(tmp_path / "node")
    for tag in ("v1.0", "v1.1"):
        for old in list(r.root.iterdir()):
            if old.name != ".vcm":
                shutil.rmtree(old) if old.is_dir() else old.unlink()
        shutil.copytree(EXAMPLES / tag, r.root, dirs_exist_ok=True)
        r.commit(f"{tag} node build", author="ops", timestamp="2026-03-01T10:00:00Z")
        r.tag(tag)
    return r


def _titles(report, cls=None):
    return [f["title"] for f in report["findings"]
            if cls is None or f["confidence_class"] == cls]


def _find(report, substring):
    return next((f for f in report["findings"] if substring in f["title"]), None)


# ---- parsing -------------------------------------------------------
def test_manifest_is_recognised_and_parsed():
    text = (EXAMPLES / "v1.1" / "gpu-node-01.node.json").read_text()
    assert looks_like_node(text)
    assert not looks_like_node('{"schema": "something-else"}')
    assert not looks_like_node("not json at all")

    comps, domains, dups, meta = parse_node(text)
    assert not dups
    assert len(comps) == 34
    assert {"PSU_BANK", "CPU0_PCIE_ROOT", "CPU1_PCIE_ROOT", "NVLINK_FABRIC"} <= domains
    gpu = next(c for c in comps if c.ref == "GPU0")
    assert gpu.value == "NVIDIA H100 NVL 94GB" and gpu.lib_id == "gpu"
    assert gpu.pads["power"] == "PSU_BANK"
    assert gpu.props["tdp_w"] == "400"          # undeclared keys are carried through
    assert "PCIE_SLOT1" in gpu.footprint        # slot plays the role a footprint does
    assert meta["chassis"]["cooling_w"] == 3600


def test_duplicate_refs_are_reported_not_silently_merged():
    text = json.dumps({"schema": "vcm-node/1", "components": [
        {"ref": "GPU0", "model": "a"}, {"ref": "GPU0", "model": "b"}]})
    comps, _, dups, _ = parse_node(text)
    assert [c.ref for c in comps] == ["GPU0"] and dups == ["GPU0"]


def test_design_exposes_components_and_domains(node_repo):
    design = design_from_revision(node_repo, node_repo.get_revision("v1.1"))
    assert len(design.components) == 34
    assert design.nets["PSU_BANK"][:2] == ["CPU0.power", "CPU1.power"]
    assert node_meta(design)["node"] == "gpu-node-01"


# ---- helpers -------------------------------------------------------
def test_redundancy_policy_decides_the_budget():
    """2+2 means two supplies carry the load, not that all four of them do."""
    assert usable_watts({"redundancy": "2+2", "watts_each": 2000}, [2000] * 4) == (4000.0,
                                                                                  "2+2 redundancy")
    assert usable_watts({"redundancy": "3+1", "watts_each": 2000}, [2000] * 4)[0] == 6000.0
    assert usable_watts({}, [2000, 2000]) == (4000.0, "no declared redundancy")


def test_version_comparison_rejects_non_numeric():
    assert _version("525.60.13") > _version("470.223.02")
    assert _version("ROCm 6.0") is None and _version("") is None


# ---- rules ---------------------------------------------------------
def test_power_budget_is_the_headline_finding(node_repo):
    rep = analyze(node_repo, "v1.0", "v1.1")
    f = _find(rep, "cannot supply the node")
    assert f is not None
    assert f["confidence_class"] == "rule_based" and f["severity"] == "critical"
    # 8x400 GPU + 2x205 CPU + 16x7.5 DIMM + 2x19 NIC + 2x11 NVMe + 400 overhead = 4190
    assert "4190 W" in f["detail"] and "4000 W usable" in f["detail"]
    assert "2+2 redundancy" in f["detail"]
    assert "psu-redundancy-pull-test" in [r["name"] for r in rep["required_retests"]]


def test_driver_minimum_is_enforced_against_the_manifest(node_repo):
    rep = analyze(node_repo, "v1.0", "v1.1")
    f = _find(rep, "driver is below the required minimum")
    assert f["severity"] == "critical"
    assert "470.223.02" in f["detail"] and "525.60.13" in f["detail"]
    assert set(f["refs"]) == {f"GPU{i}" for i in range(8)}


def test_pcie_generation_and_lane_budget(node_repo):
    rep = analyze(node_repo, "v1.0", "v1.1")
    gen = _find(rep, "negotiates down")
    assert "8 devices" in gen["title"]           # eight identical GPUs, one finding
    assert "Gen5" in gen["detail"] and "Gen4" in gen["detail"]
    lanes = _find(rep, "lane budget oversubscribed")
    assert "request 88 lanes" in lanes["detail"] and "provides 64" in lanes["detail"]


def test_connector_change_is_grouped_and_flagged(node_repo):
    rep = analyze(node_repo, "v1.0", "v1.1")
    f = _find(rep, "Power connector changed")
    assert "8-pin EPS -> 12VHPWR (16-pin)" in f["title"] and "8 devices" in f["title"]
    assert len(f["refs"]) == 8


def test_identical_components_collapse_into_one_finding(node_repo):
    rep = analyze(node_repo, "v1.0", "v1.1")
    replaced = [t for t in _titles(rep, "verified") if "replaced" in t]
    assert replaced == ["8x replaced: NVIDIA A100 PCIe 40GB -> NVIDIA H100 NVL 94GB"]
    assert len(rep["findings"]) < 25, "report should stay readable, not repeat per device"


def test_power_domains_are_not_followed_as_blast_radius(node_repo):
    """Every component sits on PSU_BANK, so following it would report the whole node."""
    rep = analyze(node_repo, "v1.0", "v1.1")
    shared = _find(rep, "share connections with")
    assert "PSU_BANK" not in shared["detail"].split("Via")[1].split(":")[0]
    assert shared["detail"].count("DIMM") == 0   # memory sits on PSU_BANK only, so is not reached


def test_board_rules_do_not_fire_on_a_node(node_repo):
    rep = analyze(node_repo, "v1.0", "v1.1")
    joined = " ".join(_titles(rep))
    for board_word in ("LDO", "dropout", "output capacitance", "footprint does not match"):
        assert board_word not in joined


def test_confidence_classes_and_evidence_survive_the_domain_change(node_repo):
    rep = analyze(node_repo, "v1.0", "v1.1")
    assert rep["summary"]["by_class"]["verified"] > 0
    assert rep["summary"]["by_class"]["rule_based"] > 0
    assert rep["summary"]["by_class"]["speculative"] > 0
    assert rep["summary"]["catalog_coverage"] == 1.0
    for f in rep["findings"]:
        assert f["confidence_class"] in ("verified", "rule_based", "speculative")
        assert f["evidence"], f"{f['id']} has no evidence"
        assert any(e["type"] == "commit" for e in f["evidence"])


def test_no_findings_when_nothing_changed(node_repo):
    rep = analyze(node_repo, "v1.1", "v1.1")
    assert not [f for f in rep["findings"] if f["confidence_class"] == "rule_based"]
