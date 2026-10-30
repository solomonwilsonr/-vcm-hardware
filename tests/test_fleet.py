import json
import shutil
from pathlib import Path

import pytest

from vcm_cli.main import main
from vcm_core import Repository, VcmError
from vcm_trace import (
    define_rack,
    fleet_query,
    import_netbox,
    place_device,
    plan_rollout,
    rack_power,
    recall,
    register_device,
)
from vcm_trace.fleet import parse_filter
from vcm_workflow import create_release

NODE = Path(__file__).resolve().parent.parent / "examples" / "gpu-node"
V10_W, V11_W = 2990.0, 4190.0  # declared draw of the sample node at each revision


@pytest.fixture
def fleet(tmp_path):
    r = Repository.init(tmp_path / "fleet")
    for v in ("v1.0", "v1.1"):
        for p in list(r.root.iterdir()):
            if p.name != ".vcm":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(NODE / v, r.root, dirs_exist_ok=True)
        r.commit(v, author="t")
        r.tag(v)
    create_release(r, "R1", "v1.0")
    create_release(r, "R2", "v1.1")
    define_rack(r, "RACK-A", power_budget_w=9000)
    define_rack(r, "RACK-B", power_budget_w=20000)
    for i in range(3):
        register_device(r, f"A{i}", "R1")
        place_device(r, f"A{i}", "RACK-A", 10 + i * 4)
        register_device(r, f"B{i}", "R1")
        place_device(r, f"B{i}", "RACK-B", 10 + i * 4)
    register_device(r, "C0", "R2")
    place_device(r, "C0", "RACK-B", 30)
    return r


def test_parse_filter():
    assert parse_filter("gpu_driver<535.104") == ("gpu_driver", "<", "535.104")
    assert parse_filter("bios = 3.4") == ("bios", "=", "3.4")
    with pytest.raises(VcmError):
        parse_filter("gpu_driver")


def test_query_by_part_and_firmware(fleet):
    a100 = fleet_query(fleet, type_="gpu", model="A100")
    assert sorted(u["serial"] for u in a100) == ["A0", "A1", "A2", "B0", "B1", "B2"]
    assert len(a100[0]["components"]) == 8
    h100 = fleet_query(fleet, model="H100 NVL")
    assert [u["serial"] for u in h100] == ["C0"]
    old = fleet_query(fleet, firmware=["gpu_driver<471"])
    assert len(old) == 7  # every unit: both revisions declare 470.223.02
    assert fleet_query(fleet, firmware=["gpu_driver>=535"]) == []
    assert [u["serial"] for u in fleet_query(fleet, rack="RACK-A", release="R1")] == [
        "A0", "A1", "A2"]


def test_recall_groups_for_action(fleet):
    res = recall(fleet, mpn="M393A4K40DB3")
    assert res["count"] == 7 and res["components"] == 7 * 16
    assert sorted(res["by_rack"]) == ["RACK-A", "RACK-B"]
    assert res["by_release"]["R2"] == ["C0"]


def test_rack_power_now_and_after(fleet):
    res = rack_power(fleet, upgrade_to="v1.1", only_release="R1")
    a = next(r for r in res["racks"] if r["rack"] == "RACK-A")
    b = next(r for r in res["racks"] if r["rack"] == "RACK-B")
    assert a["now_w"] == 3 * V10_W and a["now_status"] == "tight"
    assert a["after_w"] == 3 * V11_W and a["after_status"] == "over"
    assert b["now_w"] == 3 * V10_W + V11_W and b["after_w"] == 4 * V11_W
    assert res["over_after"] == ["RACK-A"]


def test_rollout_blocks_on_critical_findings(fleet):
    plan = plan_rollout(fleet, "v1.1", only_release="R1")
    assert plan["scheduled"] == 0 and len(plan["blocked"]) == 6
    assert any("cannot supply" in c for c in plan["blocked"][0]["critical"])


def test_rollout_waves_respect_racks_and_power(fleet):
    plan = plan_rollout(fleet, "v1.1", only_release="R1", max_per_rack=1, wave_size=10,
                        allow_critical=True)
    # RACK-A is at 8970 W of 9000 W: no unit there can take +1200 W
    assert sorted(d["serial"] for d in plan["deferred"]) == ["A0", "A1", "A2"]
    assert [[u["serial"] for u in w["units"]] for w in plan["waves"]] == [["B0"], ["B1"], ["B2"]]
    for w in plan["waves"]:
        racks = [u["rack"] for u in w["units"]]
        assert len(racks) == len(set(racks))  # at most one unit per rack per wave


def test_rollout_wave_size(fleet):
    define_rack(fleet, "RACK-A", power_budget_w=50000)
    plan = plan_rollout(fleet, "v1.1", only_release="R1", max_per_rack=2, wave_size=3,
                        allow_critical=True)
    assert [len(w["units"]) for w in plan["waves"]] == [3, 3]
    assert plan["deferred"] == []


def test_netbox_import(fleet):
    devices = {"results": [
        {"name": "gpu-a0", "serial": "A0", "rack": {"name": "NB-RACK-1"}, "position": 7,
         "site": {"name": "dc1"}},
        {"name": "B1", "serial": "", "rack": {"name": "NB-RACK-2"}, "position": 9},
        {"name": "ghost", "serial": "ZZZ", "rack": {"name": "NB-RACK-1"}},
        {"name": "A2", "serial": "A2", "rack": None},
    ]}
    racks = {"results": [{"name": "NB-RACK-1", "site": {"name": "dc1"}, "u_height": 42,
                          "custom_fields": {"power_budget_w": 17000}}]}
    res = import_netbox(fleet, devices, racks)
    assert [p["serial"] for p in res["placed"]] == ["A0", "B1"]   # by serial, then by name
    assert res["unmatched_in_netbox"] == [{"name": "ghost", "serial": "ZZZ"}]
    assert res["no_rack_in_netbox"] == ["A2"]
    assert "C0" in res["not_in_netbox"]
    a0 = fleet.load_record("devices", "A0")
    assert a0["location"] == {"rack": "NB-RACK-1", "position": 7}
    rack = next(r for r in fleet.list_records("racks") if r["name"] == "NB-RACK-1")
    assert rack["power_budget_w"] == 17000 and rack["site"] == "dc1"


def test_placement_does_not_break_passport_signature(fleet, tmp_path, monkeypatch):
    from vcm_core import signing as sg
    from vcm_trace import sign_device, verify_device
    monkeypatch.setenv("VCM_KEY_HOME", str(tmp_path / "keys"))
    k = sg.generate_key("ops")
    sg.trust_key(fleet.root, "ops", k["public_key"], "now")
    sign_device(fleet, "A0", "ops")
    place_device(fleet, "A0", "RACK-B", 40)
    assert verify_device(fleet, fleet.load_record("devices", "A0"))["signed"]


def test_cli_fleet(fleet, capsys, tmp_path):
    root = str(fleet.root)
    assert main(["-C", root, "fleet", "query", "--model", "A100", "--csv"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("serial,rack") and out.count("\n") == 7
    assert main(["-C", root, "fleet", "power", "--upgrade-to", "v1.1", "--release", "R1"]) == 1
    capsys.readouterr()
    assert main(["-C", root, "fleet", "recall", "--firmware", "bios=3.4", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 7
    assert main(["-C", root, "fleet", "recall"]) == 1  # needs criteria
    assert main(["-C", root, "fleet", "rollout", "--to", "v1.1", "--release", "R1"]) == 0
    nb = tmp_path / "devices.json"
    nb.write_text(json.dumps({"results": [{"name": "x", "serial": "B2",
                                           "rack": {"name": "R9"}, "position": 1}]}))
    assert main(["-C", root, "fleet", "netbox", "--devices", str(nb)]) == 0


def test_api_fleet(fleet):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vcm_api import create_app
    c = TestClient(create_app(str(fleet.root), insecure=True))
    q = c.get("/api/fleet/query", params=[("firmware", "gpu_driver<471"), ("rack", "RACK-A")])
    assert len(q.json()) == 3
    assert c.get("/api/fleet/recall").status_code == 422
    assert c.get("/api/fleet/power", params={"upgrade_to": "v1.1"}).json()["over_after"]
    assert c.get("/api/fleet/rollout", params={"to": "v1.1"}).json()["blocked"]
    assert len(c.get("/api/racks").json()) == 2
