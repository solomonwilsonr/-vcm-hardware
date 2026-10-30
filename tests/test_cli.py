import json

import pytest

from conftest import SAMPLE, _copy_tree
from vcm_cli.main import main


@pytest.fixture
def cli_repo(tmp_path, capsys):
    root = tmp_path / "hw"
    root.mkdir()
    assert main(["init", str(root)]) == 0
    _copy_tree(SAMPLE / "v1.0", root)
    assert main(["-C", str(root), "commit", "-m", "v1.0", "--author", "alice", "--tag", "v1.0"]) == 0
    _copy_tree(SAMPLE / "v1.1", root)
    assert main(["-C", str(root), "commit", "-m", "v1.1", "--author", "bob", "--tag", "v1.1"]) == 0
    capsys.readouterr()
    return str(root)


def run(capsys, repo, *argv):
    code = main(["-C", repo, *argv])
    return code, capsys.readouterr().out


def test_log_diff_impact(cli_repo, capsys):
    code, out = run(capsys, cli_repo, "log", "--json")
    assert code == 0 and [r["message"] for r in json.loads(out)] == ["v1.1", "v1.0"]
    code, out = run(capsys, cli_repo, "diff", "v1.0", "v1.1")
    assert "U1 replaced" in out and "footprint U1" in out
    code, out = run(capsys, cli_repo, "impact", "v1.0", "v1.1", "--json")
    assert json.loads(out)["summary"]["by_class"]["speculative"] > 0
    code, out = run(capsys, cli_repo, "impact")
    assert "## Rule-based" in out


def test_validate_and_bom_validate(cli_repo, capsys, tmp_path):
    code, out = run(capsys, cli_repo, "validate", "--rev", "v1.1", "--compare", "v1.0")
    assert code == 0 and "status: pass" in out and "vs v1.0" in out
    code, out = run(capsys, cli_repo, "bom", "validate", "--rev", "HEAD")
    assert code == 0
    bad = tmp_path / "bad.csv"
    bad.write_text("Reference,Value,Footprint\nR1,,\n")
    code, out = run(capsys, cli_repo, "bom", "validate", str(bad))
    assert code == 1 and "MISSING_VALUE" in out


def test_release_device_flow(cli_repo, capsys):
    assert run(capsys, cli_repo, "release", "create", "R1.0", "--rev", "v1.0")[0] == 0
    assert run(capsys, cli_repo, "release", "create", "R1.1", "--rev", "v1.1")[0] == 0
    code, out = run(capsys, cli_repo, "release", "verify", "R1.0")
    assert code == 0 and "OK" in out
    code, out = run(capsys, cli_repo, "device", "register", "SN-1", "--release", "R1.0")
    assert code == 0 and "QR payload" in out
    run(capsys, cli_repo, "device", "register", "SN-2", "--release", "R1.1")
    code, out = run(capsys, cli_repo, "device", "affected", "--component", "U1", "--value", "AMS1117-3.3")
    assert "SN-1" in out and "SN-2" not in out
    code, out = run(capsys, cli_repo, "device", "affected", "--revision", "v1.1", "--json")
    assert [d["serial"] for d in json.loads(out)["devices"]] == ["SN-2"]
    assert run(capsys, cli_repo, "device", "affected")[0] == 2
    code, out = run(capsys, cli_repo, "device", "passport", "SN-2", "--no-svg")
    assert json.loads(out)["release"]["name"] == "R1.1"
    code, out = run(capsys, cli_repo, "rollback", "plan", "v1.0", "--from", "v1.1")
    assert "NOT fully software-reversible" in out


def test_pr_timeline_explain_graph(cli_repo, capsys):
    code, out = run(capsys, cli_repo, "pr", "open", "--base", "v1.0", "--head", "v1.1", "--title", "T")
    assert code == 0 and "PR #1" in out
    assert "approvals: alice" in run(capsys, cli_repo, "pr", "approve", "1", "--by", "alice")[1]
    assert "T" in run(capsys, cli_repo, "pr", "list")[1]
    assert json.loads(run(capsys, cli_repo, "pr", "show", "1", "--json")[1])["id"] == 1
    assert "v1.1" in run(capsys, cli_repo, "timeline")[1]
    assert "## Physical devices" in run(capsys, cli_repo, "explain", "v1.0", "v1.1")[1]
    code, out = run(capsys, cli_repo, "graph", "query", "--component", "U1")
    assert "component U2" in out and "firmware firmware/main.c" in out


def test_errors_return_nonzero(cli_repo, capsys):
    assert main(["-C", cli_repo, "diff", "nope", "HEAD"]) == 1
    assert "unknown revision" in capsys.readouterr().err
    assert main(["-C", cli_repo, "graph", "query", "--component", "ZZ"]) == 1
    assert main(["-C", cli_repo, "commit", "-m", "again"]) == 1


def test_release_verify_fails_on_tamper(cli_repo, capsys):
    from vcm_core import Repository
    run(capsys, cli_repo, "release", "create", "R1", "--rev", "v1.0")
    repo = Repository(cli_repo)
    h = repo.get_revision("v1.0").artifacts["firmware/main.c"].hash
    p = repo.store.path(h)
    p.chmod(0o644)
    p.write_bytes(b"x")
    code, out = run(capsys, cli_repo, "release", "verify", "R1")
    assert code == 1 and "VERIFICATION FAILED" in out
