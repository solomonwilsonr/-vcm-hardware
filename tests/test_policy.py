import json
import shutil
from pathlib import Path

import pytest

from vcm_cli.main import main
from vcm_core import Repository, VcmError
from vcm_core import signing as sg
from vcm_workflow import (
    approve,
    create_release,
    evaluate_policy,
    get_pr,
    load_policy,
    open_pr,
    sign_release,
    verify_release,
)

NODE = Path(__file__).resolve().parent.parent / "examples" / "gpu-node"
POLICY = {"schema": "vcm-policy/1", "rules": [
    {"id": "no-critical", "forbid_severity": "critical"},
    {"id": "coverage", "min_catalog_coverage": 0.9},
    {"id": "known-parts", "forbid_unknown_parts": True},
    {"id": "power-needs-two", "require_approvals": 2,
     "when": {"title_matches": "power|supply|cooling"}},
    {"id": "signed", "require_signed_release": True, "stages": ["verify"]},
]}


def _repo(tmp_path, policy=POLICY, head_dir="v1.1"):
    r = Repository.init(tmp_path / "p")
    for v, extra in (("v1.0", None), (head_dir, policy)):
        for p in list(r.root.iterdir()):
            if p.name != ".vcm":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(NODE / v, r.root, dirs_exist_ok=True)
        if extra is not None:
            (r.root / "vcm-policy.json").write_text(json.dumps(extra))
        r.commit(v, author="t")
        r.tag(v)
    return r


def test_policy_is_read_from_the_revision(tmp_path):
    r = _repo(tmp_path)
    assert load_policy(r, "v1.0") is None
    assert [x["id"] for x in load_policy(r, "v1.1")["rules"]][0] == "no-critical"


def test_bad_policy_rejected(tmp_path):
    r = _repo(tmp_path, policy={"rules": [{"id": "x", "nonsense": 1}]})
    with pytest.raises(VcmError, match="no known check"):
        load_policy(r, "v1.1")


def test_evaluate_stages_and_skips():
    report = {"findings": [{"id": "R1", "title": "PSU_BANK cannot supply", "severity": "critical"}],
              "summary": {"catalog_coverage": 1.0}, "catalog": {"unknown": []}}
    res = evaluate_policy(POLICY, report, "check")
    assert not res["ok"] and [v["id"] for v in res["violations"]] == ["no-critical"]
    assert {s["id"] for s in res["skipped"]} == {"power-needs-two"}
    assert "signed" not in {x["id"] for x in res["passed"] + res["skipped"]}  # verify-only
    res = evaluate_policy(POLICY, report, "review", approvals=1)
    assert {v["id"] for v in res["violations"]} == {"no-critical", "power-needs-two"}
    quiet = {"findings": [{"id": "V1", "title": "GPU0 moved", "severity": "info"}],
             "summary": {"catalog_coverage": 1.0}, "catalog": {"unknown": []}}
    res = evaluate_policy(POLICY, quiet, "review", approvals=0)
    assert res["ok"]  # the two-approval rule only applies to power-related changes
    assert evaluate_policy(POLICY, None, "release")["skipped"]  # first revision: nothing to judge
    assert evaluate_policy(None, report, "check") == {
        "stage": "check", "policy": False, "ok": True, "violations": [], "passed": [],
        "skipped": []}


def test_release_blocked_then_overridden_with_record(tmp_path):
    r = _repo(tmp_path)
    with pytest.raises(VcmError, match="release blocked by vcm-policy.json: no-critical"):
        create_release(r, "R1", "v1.1")
    m = create_release(r, "R1", "v1.1", override_policy="lab trial, 1 node",
                       overridden_by="dana")
    assert m["policy"]["ok"] is False and m["policy"]["violations"] == ["no-critical"]
    assert m["policy"]["override"] == {"reason": "lab trial, 1 node", "by": "dana"}
    assert r.audit.events()[-1]["action"] == "policy.override"
    assert verify_release(r, "R1")["checks"][0]["ok"]  # the override is inside the signed body


def test_release_without_violations_records_pass(tmp_path):
    lenient = {"rules": [{"id": "coverage", "min_catalog_coverage": 0.5}]}
    m = create_release(_repo(tmp_path, lenient), "R1", "v1.1")
    assert m["policy"] == {"present": True, "ok": True, "violations": [], "skipped": []}


def test_verify_requires_signature_when_policy_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_KEY_HOME", str(tmp_path / "keys"))
    r = _repo(tmp_path, {"rules": [{"id": "signed", "require_signed_release": True}]})
    create_release(r, "R1", "v1.1")
    res = verify_release(r, "R1")
    assert not res["ok"] and res["problems"][0]["check"] == "policy:signed"
    k = sg.generate_key("rel")
    sg.trust_key(r.root, "rel", k["public_key"], "now")
    sign_release(r, "R1", "rel")
    assert verify_release(r, "R1")["ok"]


def test_review_readiness_includes_policy(tmp_path):
    r = _repo(tmp_path, {"rules": [{"id": "power-needs-two", "require_approvals": 2,
                                    "when": {"title_matches": "supply"}}]})
    pr = open_pr(r, "v1.0", "v1.1", "upgrade", "erin")
    approve(r, pr["id"], "frank")
    st = get_pr(r, pr["id"])["status"]
    assert not st["ready_for_merge"] and st["policy"]["violations"][0]["id"] == "power-needs-two"
    approve(r, pr["id"], "grace")
    assert get_pr(r, pr["id"])["status"]["ready_for_merge"]


def test_cli_policy_check(tmp_path, capsys):
    r = _repo(tmp_path)
    assert main(["-C", str(r.root), "policy", "v1.0", "v1.1"]) == 1
    out = capsys.readouterr().out
    assert "VIOLATION no-critical" in out and "skipped   power-needs-two" in out
    assert main(["-C", str(r.root), "release", "create", "R1", "--rev", "v1.1"]) == 1
    assert main(["-C", str(r.root), "release", "create", "R1", "--rev", "v1.1",
                 "--override-policy", "approved by change board CB-17"]) == 0
