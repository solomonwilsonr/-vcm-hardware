import pytest

from vcm_core import VcmError
from vcm_trace import register_device
from vcm_workflow import (
    add_comment,
    approve,
    close_pr,
    create_release,
    explain,
    get_pr,
    list_prs,
    open_pr,
    render_pr,
    render_timeline,
    timeline,
)


def test_pr_lifecycle(sample_repo):
    pr = open_pr(sample_repo, "v1.0", "v1.1", "Swap LDO", author="bob")
    assert pr["id"] == 1 and pr["state"] == "open"
    assert pr["diff"]["summary"]["replaced"] == 2
    assert pr["impact"]["summary"]["by_class"]["rule_based"] > 0
    assert pr["status"]["critical_findings"] and not pr["status"]["ready_for_merge"]
    pr = add_comment(sample_repo, 1, "alice", "Peak load worries me")
    pr = approve(sample_repo, 1, "alice")
    pr = approve(sample_repo, 1, "alice")  # idempotent
    assert pr["status"]["approvals"] == 1 and pr["status"]["ready_for_merge"]
    assert "Peak load worries me" in render_pr(pr)
    assert open_pr(sample_repo, "v1.0", "v1.1", "second")["id"] == 2
    assert [p["id"] for p in list_prs(sample_repo)] == [1, 2]
    close_pr(sample_repo, 1)
    with pytest.raises(VcmError):
        approve(sample_repo, 1, "carol")
    assert get_pr(sample_repo, 1)["state"] == "closed"


def test_pr_blocked_by_validation_regression(repo):
    (repo.root / "bom.csv").write_text("Reference,Value,Footprint\nR1,1k,R_0603\n")
    a = repo.commit("a")
    (repo.root / "bom.csv").write_text("Reference,Value,Footprint\nR1,1k,R_0603\nR2,,\n")
    b = repo.commit("b")
    open_pr(repo, a.id, b.id, "bad")
    pr = approve(repo, 1, "x")
    assert pr["status"]["validation_regression"] and not pr["status"]["ready_for_merge"]


def test_timeline_and_explain(sample_repo):
    create_release(sample_repo, "R1.1", "v1.1")
    register_device(sample_repo, "SN-1", "R1.1")
    items = timeline(sample_repo)
    assert [i["tags"] for i in items] == [["v1.1"], ["v1.0"]]
    assert items[0]["changes"]["replaced"] == 2 and items[0]["releases"] == ["R1.1"]
    assert items[0]["devices"] == 1 and items[1]["changed_components"][0] == "C1"
    assert "v1.1" in render_timeline(items)
    ex = explain(sample_repo, "v1.0", "v1.1")
    assert "What changed" in ex["text"] and "Speculative" in ex["text"] and "SN-1" in ex["text"]
    assert ex["impact"]["summary"]["by_class"]["verified"] > 0
