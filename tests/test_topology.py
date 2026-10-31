"""The Setup page's data: one read-only view of components, domains, machine data, units, racks."""
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from vcm_ai.rules_datacenter import node_meta  # noqa: E402
from vcm_api import create_app  # noqa: E402
from vcm_api.topology import build_topology  # noqa: E402
from vcm_core import Repository  # noqa: E402
from vcm_diff import compare_drift  # noqa: E402
from vcm_parsers import (  # noqa: E402
    design_from_revision,
    merge_observations,
    parse_nvidia_smi,
    parse_redfish,
)
from vcm_trace import define_rack, place_device, register_device  # noqa: E402
from vcm_workflow import create_release  # noqa: E402

NODE = Path(__file__).resolve().parent.parent / "examples" / "gpu-node"


@pytest.fixture
def node(tmp_path):
    r = Repository.init(tmp_path / "node")
    for v in ("v1.0", "v1.1"):
        for p in list(r.root.iterdir()):
            if p.name != ".vcm":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(NODE / v, r.root, dirs_exist_ok=True)
        r.commit(v, author="t")
        r.tag(v)
    return r


def _attach(repo, rev="v1.1"):
    obs = merge_observations([
        parse_redfish((NODE / "observed" / "redfish-capture.json").read_text()),
        parse_nvidia_smi((NODE / "observed" / "nvidia-smi.csv").read_text())])
    design = design_from_revision(repo, repo.get_revision(rev))
    res = compare_drift(design, node_meta(design), obs)
    repo.attach_evidence(repo.resolve(rev), "observed",
                         {"observation": obs, "drift": res, "captured_at": obs.get("captured_at", "")})


def test_topology_describes_every_part_and_domain(node):
    t = build_topology(node, "v1.1")
    assert t["schema"] == "vcm-topology/1"
    assert t["counts"]["by_type"] == {"cpu": 2, "dimm": 16, "gpu": 8, "nic": 2, "nvme": 2, "psu": 4}
    assert t["node"]["chassis"]["model"] == "Supermicro SYS-420GP-TNR"
    dom = {d["name"]: d for d in t["domains"]}
    assert dom["PSU_BANK"]["kind"] == "power" and dom["PSU_BANK"]["declared"]["redundancy"] == "2+2"
    assert dom["CPU0_PCIE_ROOT"]["kind"] == "pcie" and dom["NVLINK_FABRIC"]["kind"] == "fabric"
    gpu0 = next(c for c in t["components"] if c["ref"] == "GPU0")
    assert gpu0["connections"]["pcie"] == "CPU0_PCIE_ROOT" and "GPU0" in dom["CPU0_PCIE_ROOT"]["members"]
    refs = [c["ref"] for c in t["components"]]
    assert refs.index("DIMM02") < refs.index("DIMM10"), "natural order, not DIMM10 before DIMM2"


def test_without_a_capture_nothing_claims_to_match_the_machine(node):
    """Honesty: no machine data means 'unobserved', never 'matches'."""
    t = build_topology(node, "v1.1")
    assert t["observed"] is None
    assert {c["status"] for c in t["components"]} == {"unobserved"}
    assert all(c["measured_w"] is None for c in t["components"])


def test_attached_capture_marks_drift_and_measured_draw(node):
    _attach(node)
    t = build_topology(node, "v1.1")
    st = {c["ref"]: c["status"] for c in t["components"]}
    assert st["DIMM11"] == "drift"                      # the failed DIMM in the sample capture
    assert st["PSU0"] == "matches" and st["CPU0"] == "matches"
    assert t["observed"]["sources"] == ["redfish", "nvidia-smi"]
    assert t["counts"]["drift"] == sum(v == "drift" for v in st.values()) > 0
    gpu = next(c for c in t["components"] if c["ref"] == "GPU3")
    assert gpu["measured_w"] == pytest.approx(240.0)
    # firmware drift names no component, so it is listed for the node, not pinned to a part
    assert any("gpu_driver" in f["title"] for f in t["observed"]["node_findings"])
    # evidence belongs to one revision: the other one is still unobserved
    assert build_topology(node, "v1.0")["observed"] is None


def test_units_and_racks_are_included_and_the_fingerprint_tracks_change(node):
    create_release(node, "R1", "v1.1")
    define_rack(node, "RACK-A", power_budget_w=9000)
    before = build_topology(node, "v1.1")
    assert before["devices"] == [] and before["racks"][0]["placed"] == []
    assert build_topology(node, "v1.1")["fingerprint"] == before["fingerprint"], "stable when idle"
    register_device(node, "N-1", "R1")
    place_device(node, "N-1", "RACK-A", 12)
    after = build_topology(node, "v1.1")
    assert after["fingerprint"] != before["fingerprint"]
    assert after["devices"][0]["on_revision"] is True
    assert after["racks"][0]["placed"] == [{"serial": "N-1", "position": 12}]


def test_topology_endpoint_is_read_only_and_validates_the_revision(node):
    events = len(node.audit.events())
    c = TestClient(create_app(str(node.root), insecure=True))
    r = c.get("/api/topology", params={"rev": "v1.1"})
    assert r.status_code == 200 and r.json()["revision"]["tags"] == ["v1.1"]
    assert c.get("/api/topology").json()["revision"]["id"] == node.resolve("HEAD")
    assert c.get("/api/topology", params={"rev": "nope"}).status_code == 404
    assert len(node.audit.events()) == events, "reading the setup must not change state"
    assert json.dumps(r.json())  # plain JSON all the way down


def test_topology_needs_a_signed_in_viewer(node):
    from vcm_core.tokens import TokenStore
    tok = TokenStore(node.dir).create("v", "viewer", "2026-01-01T00:00:00Z")
    c = TestClient(create_app(str(node.root)))
    assert c.get("/api/topology").status_code == 401
    assert c.get("/api/topology", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.post("/api/topology", headers={"Authorization": f"Bearer {tok}"}).status_code in (403, 405)


def test_setup_page_is_wired_into_the_dashboard():
    static = Path(__file__).resolve().parent.parent / "services" / "api" / "vcm_api" / "static"
    js, page = (static / "app.js").read_text(), (static / "index.html").read_text()
    assert 'data-view="setup"' in page and "setup: setupView" in js
    assert "function setupView(" in js and '"/api/topology?rev="' in js
    assert "clearInterval(st.timer)" in js, "the poller must stop when the page is left"
    assert "if (S.ui && S.ui.stop) S.ui.stop();" in js
