"""Fleet operations stay fast at fleet size.

Unit records are written directly (QR rendering, a one-off per-unit cost at registration, is not
what is being measured). Thresholds are generous so the test catches accidental quadratic
behaviour, not machine-to-machine noise.
"""
import copy
import json
import time
from pathlib import Path

from vcm_core import Repository
from vcm_trace import define_rack, fleet_query, plan_rollout, rack_power, recall
from vcm_workflow import create_release

BASE = json.loads((Path(__file__).resolve().parent.parent / "examples" / "gpu-node" / "v1.0"
                   / "gpu-node-01.node.json").read_text())
DRIVERS = ["470.223.02", "525.60.13", "535.129.03", "550.54.15", "560.35.03"]
UNITS, RACKS = 2000, 50


def _timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def test_fleet_queries_scale(tmp_path):
    r = Repository.init(tmp_path / "fleet")
    revs = []
    for i, drv in enumerate(DRIVERS):
        m = copy.deepcopy(BASE)
        m["firmware"]["gpu_driver"] = drv
        (r.root / "node.node.json").write_text(json.dumps(m))
        revs.append(r.commit(f"driver {drv}", author="t").id)
        r.tag(f"t{i}")
        create_release(r, f"R{i}", f"t{i}")
    for k in range(RACKS):
        define_rack(r, f"RK{k:02d}", power_budget_w=200000)
    for n in range(UNITS):
        r.save_record("devices", f"N{n:05d}", {
            "serial": f"N{n:05d}", "release": f"R{n % 5}", "revision": revs[n % 5], "batch": "",
            "notes": "", "registered_at": "2026-01-01T00:00:00Z", "qr_payload": "",
            "location": {"rack": f"RK{n % RACKS:02d}", "position": n % 40}})

    old, t_query = _timed(lambda: fleet_query(r, type_="gpu", firmware=["gpu_driver<535"]))
    assert len(old) == UNITS * 2 // 5 and t_query < 5
    rec, t_recall = _timed(lambda: recall(r, mpn="M393A4K40DB3"))
    assert rec["count"] == UNITS and rec["components"] == UNITS * 16 and t_recall < 5
    power, t_power = _timed(lambda: rack_power(r, upgrade_to="t4"))
    assert len(power["racks"]) == RACKS and t_power < 5
    plan, t_plan = _timed(lambda: plan_rollout(r, "t4", max_per_rack=4, wave_size=200,
                                               allow_critical=True))
    assert plan["scheduled"] == UNITS * 4 // 5 and t_plan < 15
    assert all(len(w["units"]) <= 200 for w in plan["waves"])
