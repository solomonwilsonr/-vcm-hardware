import copy
import json
from pathlib import Path

import pytest

from vcm_ai import analyze
from vcm_ai.catalog import Catalog, CatalogError, review_part, unverified_fields
from vcm_cli.main import main
from vcm_core import Repository

BASE = json.loads((Path(__file__).resolve().parent.parent / "examples" / "gpu-node" / "v1.0"
                   / "gpu-node-01.node.json").read_text())


def _comp(m, ref):
    return next(c for c in m["components"] if c["ref"] == ref)


def _impact(tmp_path, mutate, base_mutate=None):
    """Commit the base manifest (optionally adjusted), then the mutated one; analyse the change."""
    repo = Repository.init(tmp_path / "n")
    path = repo.root / "gpu-node-01.node.json"
    base = copy.deepcopy(BASE)
    if base_mutate:
        base_mutate(base)
    path.write_text(json.dumps(base))
    repo.commit("base", author="t", timestamp="2026-01-01T00:00:00Z")
    head = copy.deepcopy(base)
    mutate(head)
    path.write_text(json.dumps(head))
    repo.commit("head", author="t", timestamp="2026-01-02T00:00:00Z")
    return analyze(repo, "HEAD~1", "HEAD")


def _titles(rep, cls="rule_based"):
    return {f["title"]: f for f in rep["findings"] if f["confidence_class"] == cls}


def _ddr5(m, ref):
    c = _comp(m, ref)
    c.update(model="64GB DDR5-4800 RDIMM", mpn="M321R8GA0BB0-CQK", form_factor="DDR5 RDIMM")


def test_ddr_generation_mismatch_is_critical(tmp_path):
    rep = _impact(tmp_path, lambda m: [_ddr5(m, f"DIMM0{i}") for i in range(8)])
    f = next(f for t, f in _titles(rep).items() if "DDR5 modules on a DDR4 CPU" in t)
    assert f["severity"] == "critical" and "CPU0" in f["refs"]
    db = [e for e in f["evidence"] if e["type"] == "db"]
    assert db[0]["fields"]["memory_type"]["from"] == "catalog"


def test_mixed_modules_on_one_controller(tmp_path):
    def mutate(m):
        _comp(m, "DIMM00").update(model="64GB DDR4-3200 RDIMM", mpn="M393A8G40AB2-CWE")
    rep = _impact(tmp_path, mutate)
    assert any("mixed memory modules" in t for t in _titles(rep))
    assert not any("DDR4 CPU" in t for t in _titles(rep))  # same generation: no fit problem


def test_uneven_channel_population(tmp_path):
    def mutate(m):
        m["components"] = [c for c in m["components"] if c["ref"] != "DIMM07"]
    rep = _impact(tmp_path, mutate)
    f = next(f for t, f in _titles(rep).items() if "7 modules across 8 channels" in t)
    assert f["severity"] == "warning"


def test_socket_and_mixed_cpu(tmp_path):
    def mutate(m):
        _comp(m, "CPU0").update(model="AMD EPYC 9654", mpn="")
    t = _titles(_impact(tmp_path, mutate))
    assert any("needs socket SP5, board has LGA4189" in x for x in t)
    assert any(x.startswith("Mixed processors across sockets") for x in t)


def test_mixed_accelerators_on_one_fabric(tmp_path):
    def mutate(m):
        _comp(m, "GPU0").update(model="NVIDIA H100 NVL 94GB", mpn="900-21010-0000-000")
    t = _titles(_impact(tmp_path, mutate))
    f = next(f for x, f in t.items() if x.startswith("NVLINK_FABRIC bridges different"))
    assert f["severity"] == "critical" and len(f["refs"]) == 8


def test_nic_speed_against_declared_fabric(tmp_path):
    def base(m):
        m["fabrics"] = {"ETH_FABRIC": {"speed_gbps": 200}}

    def mutate(m):
        _comp(m, "NIC0").update(model="ConnectX-7 400G", mpn="MCX755106AS")
    t = _titles(_impact(tmp_path, mutate, base))
    f = next(f for x, f in t.items() if "400 Gb/s port on a 200 Gb/s fabric" in x)
    assert f["severity"] == "info"  # faster than the fabric: links down, nothing breaks


def test_slower_nic_is_a_warning(tmp_path):
    def base(m):
        m["fabrics"] = {"ETH_FABRIC": {"speed_gbps": 400}}

    def mutate(m):
        _comp(m, "NIC0")["slot"] = "OCP_SLOT9"  # touch NIC0 so the rule considers it
    f = next(f for x, f in _titles(_impact(tmp_path, mutate, base)).items() if "Gb/s fabric" in x)
    assert f["severity"] == "warning"


def test_cuda_minimum_is_checked(tmp_path):
    def mutate(m):
        for i in range(8):
            _comp(m, f"GPU{i}").update(model="NVIDIA H100 NVL 94GB", mpn="900-21010-0000-000")
    t = _titles(_impact(tmp_path, mutate))
    f = next(f for x, f in t.items() if x.startswith("CUDA runtime 11.4 is below"))
    assert f["severity"] == "critical" and len(f["refs"]) == 8


def test_too_many_accelerators_for_the_chassis(tmp_path):
    def mutate(m):
        extra = copy.deepcopy(_comp(m, "GPU7"))
        extra.update(ref="GPU8", slot="PCIE_SLOT9")
        m["components"].append(extra)
    assert any(x.startswith("9 accelerators in a chassis with 8 slots")
               for x in _titles(_impact(tmp_path, mutate)))


def test_findings_cite_values_field_by_field(tmp_path):
    def mutate(m):
        for i in range(8):
            _comp(m, f"GPU{i}").update(model="NVIDIA H100 NVL 94GB", mpn="900-21010-0000-000")
    f = next(f for x, f in _titles(_impact(tmp_path, mutate)).items()
             if "driver is below" in x)
    db = next(e for e in f["evidence"] if e["type"] == "db")
    assert db["fields"]["min_driver"]["from"] == "catalog"
    assert db["fields"]["min_driver"]["verified_by"] == ""
    assert db["reviewed"] is False


# ---- review workflow -----------------------------------------------------------------
def _h100():
    return Catalog.load(bundled=True).find("900-21010-0000-000")


def test_review_marks_part_reviewed_only_when_every_field_is_checked():
    part = _h100()
    fields = unverified_fields(part)
    assert "tdp_w" in fields and "min_driver" in fields
    partial = review_part(part, {"tdp_w": 400.0}, "datasheet p.3", "alice", "2026-10-30")
    assert partial["reviewed"] is False
    assert partial["sources"]["tdp_w"]["verified_by"] == "alice"
    full = review_part(part, {}, "datasheet p.3", "alice", "2026-10-30", confirm=fields)
    assert full["reviewed"] is True and not unverified_fields(full)


def test_review_requires_source_and_reviewer():
    with pytest.raises(CatalogError):
        review_part(_h100(), {"tdp_w": 1}, "", "alice", "now")
    with pytest.raises(CatalogError):
        review_part(_h100(), {"bogus": 1}, "ds", "alice", "now")


def test_cli_review_flows_into_findings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["catalog", "review", "900-21010-0000-000", "--by", "alice",
                 "--source", "H100 NVL datasheet DS-11717 p.3", "--confirm", "min_driver"]) == 0
    saved = json.loads((tmp_path / "vcm-catalog" / "reviews.json").read_text())["parts"]
    assert saved[0]["sources"]["min_driver"]["verified_by"] == "alice"
    assert "still unverified" in capsys.readouterr().out

    repo = Repository.init(tmp_path)
    path = tmp_path / "gpu-node-01.node.json"
    path.write_text(json.dumps(BASE))
    repo.commit("base", author="t")
    head = copy.deepcopy(BASE)
    for i in range(8):
        _comp(head, f"GPU{i}").update(model="NVIDIA H100 NVL 94GB", mpn="900-21010-0000-000")
    path.write_text(json.dumps(head))
    repo.commit("head", author="t")
    rep = analyze(repo, "HEAD~1", "HEAD")
    f = next(f for f in rep["findings"] if "driver is below" in f["title"])
    db = next(e for e in f["evidence"] if e["type"] == "db")
    assert db["fields"]["min_driver"]["verified_by"] == "alice"
    assert main(["catalog", "unreviewed", "--type", "gpu"]) == 0
