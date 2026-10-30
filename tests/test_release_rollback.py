import json

import pytest

from vcm_core import VcmError
from vcm_workflow import create_release, plan_rollback, render_plan, to_yaml, verify_release


def test_release_create_and_verify(sample_repo, tmp_path):
    m = create_release(sample_repo, "R1.1", "v1.1", out=tmp_path / "r.json")
    assert m["revision"] == sample_repo.resolve("v1.1")
    assert m["validation"]["status"] == "pass" and m["toolchain"]["python"]
    assert "firmware/main.c" in m["artifacts"]
    res = verify_release(sample_repo, "R1.1")
    assert res["ok"] and not res["problems"]
    assert verify_release(sample_repo, str(tmp_path / "r.json"))["ok"]


def test_release_tamper_blob_detected(sample_repo):
    m = create_release(sample_repo, "R1.0", "v1.0")
    h = m["artifacts"]["firmware/main.c"]["hash"]
    p = sample_repo.store.path(h)
    p.chmod(0o644)
    p.write_bytes(b"int main(){ evil(); }")
    res = verify_release(sample_repo, "R1.0")
    assert not res["ok"]
    assert [c["check"] for c in res["problems"]] == ["artifact:firmware/main.c"]


def test_release_tamper_manifest_detected(sample_repo, tmp_path):
    create_release(sample_repo, "R1.0", "v1.0", out=tmp_path / "r.json")
    data = json.loads((tmp_path / "r.json").read_text())
    data["artifacts"]["firmware/main.c"]["hash"] = "0" * 64
    (tmp_path / "bad.json").write_text(json.dumps(data))
    res = verify_release(sample_repo, str(tmp_path / "bad.json"))
    names = {c["check"] for c in res["problems"]}
    assert not res["ok"] and "manifest_hash" in names and "artifact_set" in names


def test_release_worktree_check(sample_repo):
    create_release(sample_repo, "R1.1", "v1.1")
    assert verify_release(sample_repo, "R1.1", worktree=True)["ok"]
    (sample_repo.root / "firmware" / "main.c").write_text("changed")
    res = verify_release(sample_repo, "R1.1", worktree=True)
    assert [c["check"] for c in res["problems"]] == ["worktree:firmware/main.c"]


def test_release_duplicates_and_names(sample_repo):
    create_release(sample_repo, "R1", "v1.0")
    with pytest.raises(VcmError):
        create_release(sample_repo, "R1", "v1.0")
    with pytest.raises(VcmError):
        create_release(sample_repo, "../evil", "v1.0")


def test_yaml_output_parses(sample_repo, tmp_path):
    yaml = pytest.importorskip("yaml")
    m = create_release(sample_repo, "Y1", "v1.0", out=tmp_path / "r.yaml")
    assert yaml.safe_load((tmp_path / "r.yaml").read_text()) == m
    assert to_yaml({"a": [], "b": {}, "c": [1, {"d": None}]})


def test_rollback_hardware_change_needs_respin(sample_repo):
    plan = plan_rollback(sample_repo, "v1.0", "v1.1")
    assert not plan["fully_software_reversible"]
    assert {x["ref"] for x in plan["respin_required"] if x["kind"] == "replace_different_footprint"} == {"U1", "C2"}
    assert not plan["software_reversible"]  # firmware identical in both revisions
    text = render_plan(plan)
    assert "Board respin required" in text and "SOT-223" in text


def test_rollback_firmware_only_is_software_reversible(sample_repo):
    r = sample_repo
    (r.root / "firmware" / "config.h").write_text("#define BROWNOUT_THRESHOLD_MV 2700\n")
    r.commit("tune brownout", timestamp="2026-03-01T00:00:00Z")
    plan = plan_rollback(r, "v1.1", "HEAD")
    assert plan["fully_software_reversible"]
    assert plan["software_reversible"][0]["path"] == "firmware/config.h"
    assert "reflash" in plan["software_reversible"][0]["action"]
    assert not plan["physical_rework"] and not plan["respin_required"]


def test_rollback_same_footprint_swap_is_rework(repo):
    csv = "Reference,Value,Footprint,MPN\nR1,{v},R_0603,{m}\n"
    (repo.root / "bom.csv").write_text(csv.format(v="1k", m="A"))
    a = repo.commit("a")
    (repo.root / "bom.csv").write_text(csv.format(v="2k", m="B"))
    b = repo.commit("b")
    plan = plan_rollback(repo, a.id, b.id)
    assert plan["physical_rework"][0]["kind"] == "replace" and not plan["respin_required"]
    assert "Desolder R1" in plan["physical_rework"][0]["instruction"]
