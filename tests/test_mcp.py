import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from vcm_core import Repository, VcmError
from vcm_mcp import approve, get_proposal, handle, list_proposals, propose, reject
from vcm_mcp.proposals import MAX_PENDING_PER_PROPOSER
from vcm_mcp.tools import TOOLS, render
from vcm_trace import place_device, register_device
from vcm_workflow import create_release

ROOT = Path(__file__).resolve().parent.parent
NODE = ROOT / "examples" / "gpu-node"
OBS = NODE / "observed"


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch):
    monkeypatch.setenv("VCM_AGENT_NAME", "claude")
    monkeypatch.setenv("VCM_AUTHOR", "alice")


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
    register_device(r, "N1", "R1")
    place_device(r, "N1", "RACK-A", 4)
    return r


def rpc(repo, method, params=None, rid=1):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return handle(msg, str(repo.root))


def call(repo, name, args=None):
    res = rpc(repo, "tools/call", {"name": name, "arguments": args or {}})["result"]
    body = res["content"][0]["text"]
    return res["isError"], (body if res["isError"] else json.loads(body))


def state(repo):
    """Everything an agent must not change through a read: refs, records, tree, audit log."""
    h = hashlib.sha256()
    for sub in ("refs", "releases", "devices", "racks", "prs", "audit", "proposals", "evidence"):
        base = repo.dir / sub
        for p in sorted(base.rglob("*")) if base.is_dir() else []:
            if p.is_file():
                h.update(p.relative_to(repo.dir).as_posix().encode() + p.read_bytes())
    h.update((repo.dir / "HEAD").read_bytes())
    for rel, path in sorted(repo.scan().items()):
        h.update(rel.encode() + path.read_bytes())
    return h.hexdigest()


# ---- protocol ----------------------------------------------------------------------
def test_initialize_negotiates_and_announces(fleet):
    r = rpc(fleet, "initialize", {"protocolVersion": "2024-11-05"})["result"]
    assert r["protocolVersion"] == "2024-11-05" and "tools" in r["capabilities"]
    assert "never upgrade it" in r["instructions"]
    assert rpc(fleet, "initialize", {"protocolVersion": "1999-01-01"})["result"][
        "protocolVersion"] == "2025-06-18"


def test_notifications_get_no_reply_and_bad_input_gets_errors(fleet):
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, str(fleet.root)) is None
    assert rpc(fleet, "nope")["error"]["code"] == -32601
    assert handle([], str(fleet.root))["error"]["code"] == -32600
    assert rpc(fleet, "tools/call", {"name": "vcm_nope"})["error"]["code"] == -32602
    assert rpc(fleet, "ping")["result"] == {}


def test_tools_declare_what_they_do(fleet):
    tools = {t["name"]: t for t in rpc(fleet, "tools/list")["result"]["tools"]}
    writers = [n for n, t in tools.items() if not t["annotations"]["readOnlyHint"]]
    assert writers == ["vcm_propose"]
    assert all(t["annotations"]["destructiveHint"] is False for t in tools.values())
    assert all(t["inputSchema"]["additionalProperties"] is False for t in tools.values())
    # an agent has no tool to decide, sign, review, override, commit or push
    forbidden = {"approve", "reject", "sign", "review", "override", "commit", "push", "lock"}
    assert not [n for n in tools if forbidden & set(n.split("_"))]


def test_prompts(fleet):
    names = {p["name"] for p in rpc(fleet, "prompts/list")["result"]["prompts"]}
    assert names == {"review_change", "fleet_upgrade_plan"}
    text = rpc(fleet, "prompts/get", {"name": "review_change",
                                      "arguments": {"base": "v1.0", "head": "v1.1"}}
               )["result"]["messages"][0]["content"]["text"]
    assert "v1.0 to v1.1" in text and "do not describe a rule_based" in text
    assert "error" in rpc(fleet, "prompts/get", {"name": "review_change", "arguments": {}})


# ---- reading -----------------------------------------------------------------------
def test_read_only_tools_change_nothing(fleet):
    before = state(fleet)
    reads = {
        "vcm_repo_info": {}, "vcm_timeline": {}, "vcm_diff": {"a": "v1.0", "b": "v1.1"},
        "vcm_impact": {"a": "v1.0", "b": "v1.1"}, "vcm_explain": {"a": "v1.0", "b": "v1.1"},
        "vcm_graph_affected": {"component": "GPU0", "rev": "v1.1"},
        "vcm_catalog_lookup": {"part": "NVIDIA H100 NVL 94GB"}, "vcm_catalog_unreviewed": {},
        "vcm_fleet_query": {"type": "gpu"}, "vcm_fleet_recall": {"mpn": "M393"},
        "vcm_rack_power": {"upgrade_to": "v1.1"}, "vcm_rollout_plan": {"to": "v1.1"},
        "vcm_policy_check": {"a": "v1.0", "b": "v1.1"}, "vcm_release_verify": {"name": "R1"},
        "vcm_audit_verify": {}, "vcm_device_passport": {"serial": "N1"},
        "vcm_rollback_plan": {"target": "v1.0", "current": "v1.1"}, "vcm_list_proposals": {},
        "vcm_drift": {"rev": "v1.0", "redfish": (OBS / "redfish-capture.json").read_text(),
                      "nvidia_smi": (OBS / "nvidia-smi.csv").read_text()},
        "vcm_draft_manifest": {"nvidia_smi": (OBS / "nvidia-smi.csv").read_text()},
    }
    assert set(reads) == {n for n, t in TOOLS.items() if not t.writes}  # none left untested
    for name, args in reads.items():
        err, out = call(fleet, name, args)
        assert not err, (name, out)
    assert state(fleet) == before


def test_impact_is_compact_and_keeps_confidence_labels(fleet):
    _, rep = call(fleet, "vcm_impact", {"a": "v1.0", "b": "v1.1"})
    assert rep["schema"] == "vcm-impact/1" and "never present" in rep["how_to_read"].lower()
    assert {f["confidence_class"] for f in rep["findings"]} == {"verified", "rule_based",
                                                                "speculative"}
    f = next(f for f in rep["findings"] if "cannot supply" in f["title"])
    assert f["severity"] == "critical" and f["evidence"] and f["evidence_count"] >= 1
    _, crit = call(fleet, "vcm_impact", {"a": "v1.0", "b": "v1.1", "min_severity": "critical",
                                         "classes": ["rule_based"]})
    assert 0 < crit["findings_shown"] < rep["findings_total"]
    assert all(x["severity"] == "critical" and x["confidence_class"] == "rule_based"
               for x in crit["findings"])


def test_drift_takes_inline_captures_and_reports_facts(fleet):
    _, res = call(fleet, "vcm_drift", {"rev": "v1.0", "redfish": (OBS / "redfish-capture.json"
                                                                  ).read_text()})
    assert res["status"] == "drifted"
    assert any(f["kind"] == "missing" and f["refs"] == ["DIMM11"] for f in res["findings"])


def test_bad_calls_are_errors_not_crashes(fleet):
    assert call(fleet, "vcm_diff", {"a": "v1.0"})[0]                       # missing b
    assert call(fleet, "vcm_diff", {"a": "v1.0", "b": "nope"})[0]          # unknown revision
    assert call(fleet, "vcm_diff", {"a": "v1.0", "b": "v1.1", "x": 1})[0]  # extra argument
    assert call(fleet, "vcm_timeline", {"limit": "ten"})[0]                # wrong type
    assert call(fleet, "vcm_fleet_recall", {})[0]                          # needs criteria
    assert call(fleet, "vcm_drift", {})[0]                                 # nothing to compare


def test_oversized_results_are_truncated_not_dumped():
    out = json.loads(render({"x": "y" * 200_000}))
    assert out["truncated"] and out["original_characters"] > 200_000
    assert len(json.dumps(out)) < 61_000


# ---- proposing ---------------------------------------------------------------------
def test_proposal_changes_nothing_until_a_person_approves(fleet):
    before = state(fleet)
    err, out = call(fleet, "vcm_propose", {
        "action": "fleet.place", "args": {"serial": "N1", "rack": "RACK-B", "position": 9},
        "rationale": "RACK-A is at 90% of budget (finding R001)"})
    assert not err and out["status"] == "pending" and "vcm proposal approve" in out["next"]
    after = state(fleet)
    assert after != before                        # the proposal record and its audit event
    assert fleet.load_record("devices", "N1")["location"]["rack"] == "RACK-A"   # but no change
    rec = get_proposal(fleet, out["proposal"])
    assert rec["proposer"] == "agent:claude" and rec["status"] == "pending"
    assert fleet.audit.events()[-1]["action"] == "proposal.create"
    assert fleet.audit.events()[-1]["actor"] == "agent:claude"

    done = approve(fleet, out["proposal"], "dana")
    assert done["status"] == "approved" and done["decided_by"] == "dana"
    assert fleet.load_record("devices", "N1")["location"] == {"rack": "RACK-B", "position": 9}
    actions = [(e["action"], e["actor"]) for e in fleet.audit.events()[-3:]]
    assert ("device.place", "dana") in actions and ("proposal.approve", "dana") in actions
    assert os.environ["VCM_AUTHOR"] == "alice"   # the approver's identity was only borrowed


def test_agents_and_proposers_cannot_decide(fleet):
    rec = propose(fleet, "rack.define", {"name": "RACK-Z", "power_budget_w": 9000}, "new rack")
    for who in ("agent:claude", "agent:other", ""):
        with pytest.raises(VcmError, match="person|made it"):
            approve(fleet, rec["id"], who)
    with pytest.raises(VcmError):
        reject(fleet, rec["id"], "agent:claude")
    assert get_proposal(fleet, rec["id"])["status"] == "pending"
    approve(fleet, rec["id"], "dana")
    with pytest.raises(VcmError, match="already approved"):
        approve(fleet, rec["id"], "erin")


def test_reject_records_a_reason_and_does_nothing(fleet):
    rec = propose(fleet, "rack.define", {"name": "RACK-Z"}, "new rack")
    reject(fleet, rec["id"], "dana", "not now")
    got = get_proposal(fleet, rec["id"])
    assert got["status"] == "rejected" and got["reason"] == "not now"
    assert not (fleet.dir / "racks" / "RACK-Z.json").exists()


def test_proposals_are_validated_when_made_and_again_when_approved(fleet):
    bad = [
        ("release.create", {"name": "R1", "rev": "v1.1"}),                  # name taken
        ("release.create", {"name": "R9", "rev": "v1.1", "override": "x"}),  # no overrides
        ("fleet.place", {"serial": "NOPE", "rack": "A"}),
        ("fleet.place", {"serial": "N1", "rack": "A", "position": -1}),
        ("fleet.place", {"serial": "N1", "rack": "A", "position": True}),
        ("rack.define", {"name": "../escape"}),
        ("rack.define", {"name": "R", "power_budget_w": -5}),
        ("device.register", {"serial": "N1", "release": "R1"}),              # already registered
        ("device.register", {"serial": "N2", "release": "R404"}),
        ("pr.open", {"base": "v1.0", "head": "v1.0", "title": "x"}),
        ("sign.release", {"name": "R1"}),                                    # not an action
        ("catalog.review", {"part": "x"}),                                   # not an action
    ]
    for action, args in bad:
        with pytest.raises(VcmError):
            propose(fleet, action, args, "because")
    with pytest.raises(VcmError, match="rationale"):
        propose(fleet, "rack.define", {"name": "R"}, "  ")
    # something valid when proposed can stop being valid before a person looks at it
    rec = propose(fleet, "device.register", {"serial": "N2", "release": "R1"}, "new unit")
    register_device(fleet, "N2", "R1")
    with pytest.raises(VcmError, match="already registered"):
        approve(fleet, rec["id"], "dana")
    assert get_proposal(fleet, rec["id"])["status"] == "pending"


def test_a_release_that_policy_would_block_cannot_even_be_proposed(tmp_path):
    r = Repository.init(tmp_path / "gated")
    shutil.copytree(NODE / "v1.0", r.root, dirs_exist_ok=True)
    r.commit("base", author="t")
    for p in list(r.root.iterdir()):
        if p.name != ".vcm":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    shutil.copytree(NODE / "v1.1", r.root, dirs_exist_ok=True)
    (r.root / "vcm-policy.json").write_text(json.dumps(
        {"rules": [{"id": "no-critical", "forbid_severity": "critical"}]}))
    r.commit("upgrade, with a policy that forbids what it breaks", author="t")
    with pytest.raises(VcmError, match="blocked by vcm-policy.json.*cannot be proposed"):
        propose(r, "release.create", {"name": "R2", "rev": "HEAD"}, "ship it")
    assert not list_proposals(r)


def test_release_and_review_proposals_run_as_the_approver(fleet):
    rec = propose(fleet, "release.create", {"name": "R2", "rev": "v1.1"}, "pin the upgrade")
    assert rec["args"]["rev"] == fleet.resolve("v1.1")       # resolved at proposal time
    approve(fleet, rec["id"], "dana")
    assert fleet.load_record("releases", "R2")["revision"] == fleet.resolve("v1.1")
    rec = propose(fleet, "pr.open", {"base": "v1.0", "head": "v1.1", "title": "upgrade"}, "review")
    out = approve(fleet, rec["id"], "dana")
    pr = fleet.load_record("prs", str(out["result"]["pr"]))
    assert pr["author"] == "agent:claude (approved by dana)"


def test_proposal_flood_is_capped(fleet):
    for i in range(MAX_PENDING_PER_PROPOSER):
        propose(fleet, "rack.define", {"name": f"R{i}"}, "x")
    with pytest.raises(VcmError, match="pending proposals"):
        propose(fleet, "rack.define", {"name": "one-more"}, "x")
    assert len(list_proposals(fleet, "pending")) == MAX_PENDING_PER_PROPOSER


# ---- the real thing: a subprocess speaking the protocol over stdio -----------------
def test_stdio_server_end_to_end(fleet):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        str(p) for p in [*(ROOT / "packages").iterdir(), *(ROOT / "services").iterdir()])}
    proc = subprocess.Popen([sys.executable, "-m", "vcm_mcp", "--repo", str(fleet.root)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "vcm_impact", "arguments": {"a": "v1.0", "b": "v1.1",
                                                        "min_severity": "critical"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "vcm_propose", "arguments": {
             "action": "rack.define", "args": {"name": "RACK-X"}, "rationale": "demo"}}},
    ]
    stdin = "\n".join(json.dumps(m) for m in msgs) + "\nnot json\n"
    out, err = proc.communicate(stdin, timeout=120)
    assert proc.returncode == 0, err
    replies = [json.loads(line) for line in out.splitlines()]
    by_id = {r.get("id"): r for r in replies}
    assert len(replies) == 5                       # the notification got no reply
    assert by_id[1]["result"]["serverInfo"]["name"] == "vcm-hardware"
    assert len(by_id[2]["result"]["tools"]) == len(TOOLS)
    impact = json.loads(by_id[3]["result"]["content"][0]["text"])
    assert impact["findings_shown"] >= 1
    assert json.loads(by_id[4]["result"]["content"][0]["text"])["status"] == "pending"
    assert by_id[None]["error"]["code"] == -32700  # garbage line: parse error, server kept going
    assert not (fleet.dir / "racks" / "RACK-X.json").exists()   # proposed, not done


def test_server_refuses_a_non_repository(tmp_path):
    from vcm_mcp import serve
    assert serve(str(tmp_path), stdin=iter([]), stdout=None) == 1


# ---- the human side: CLI and API ---------------------------------------------------
def test_cli_proposal_flow(fleet, capsys, monkeypatch):
    from vcm_cli.main import main
    root = str(fleet.root)
    rec = propose(fleet, "rack.define", {"name": "RACK-C", "power_budget_w": 12000}, "capacity")
    assert main(["-C", root, "proposal", "list", "--status", "pending"]) == 0
    assert rec["id"] in capsys.readouterr().out
    # a script cannot approve by accident: no terminal and no --yes means no approval
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["-C", root, "proposal", "approve", rec["id"], "--by", "dana"]) == 1
    assert get_proposal(fleet, rec["id"])["status"] == "pending"
    assert main(["-C", root, "proposal", "approve", rec["id"], "--by", "agent:claude",
                 "--yes"]) == 1                       # an agent name cannot decide
    assert main(["-C", root, "proposal", "approve", rec["id"], "--by", "dana", "--yes"]) == 0
    assert (fleet.dir / "racks" / "RACK-C.json").exists()
    other = propose(fleet, "rack.define", {"name": "RACK-D"}, "more")
    assert main(["-C", root, "proposal", "reject", other["id"], "--by", "dana",
                 "--reason", "no budget"]) == 0
    assert main(["-C", root, "proposal", "show", other["id"]]) == 0
    shown = capsys.readouterr().out
    assert "rejected by dana" in shown and "no budget" in shown


def test_api_proposals_need_an_approver_identity(fleet):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vcm_api import create_app
    from vcm_core.tokens import TokenStore
    store = TokenStore(fleet.dir)
    tok = {r: store.create(f"{r}-user", r, "now") for r in ("viewer", "editor", "approver")}
    h = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731
    c = TestClient(create_app(str(fleet.root)))
    rec = propose(fleet, "rack.define", {"name": "RACK-E"}, "why")
    assert c.get("/api/proposals", headers=h(tok["viewer"])).json()[0]["id"] == rec["id"]
    assert c.get(f"/api/proposals/{rec['id']}", headers=h(tok["viewer"])).status_code == 200
    for role in ("viewer", "editor"):
        assert c.post(f"/api/proposals/{rec['id']}/approve",
                      headers=h(tok[role])).status_code == 403
    ok = c.post(f"/api/proposals/{rec['id']}/approve", headers=h(tok["approver"])).json()
    assert ok["decided_by"] == "approver-user" and ok["status"] == "approved"
    assert (fleet.dir / "racks" / "RACK-E.json").exists()
    # insecure mode has no identity, so nobody can be recorded as having decided
    insecure = TestClient(create_app(str(fleet.root), insecure=True))
    rec2 = propose(fleet, "rack.define", {"name": "RACK-F"}, "why")
    assert insecure.post(f"/api/proposals/{rec2['id']}/approve").status_code == 403
    assert get_proposal(fleet, rec2["id"])["status"] == "pending"
