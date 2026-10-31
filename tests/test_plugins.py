import shutil
import sys
from pathlib import Path

import pytest

from vcm_ai import analyze, register_rule, unregister_rule
from vcm_core import Repository

ROOT = Path(__file__).resolve().parent.parent
NODE = ROOT / "examples" / "gpu-node"


@pytest.fixture
def node_repo(tmp_path):
    r = Repository.init(tmp_path / "node")
    for v in ("v1.0", "v1.1"):
        for p in list(r.root.iterdir()):
            if p.name != ".vcm":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(NODE / v, r.root, dirs_exist_ok=True)
        r.commit(v, author="t")
        r.tag(v)
    return r


@pytest.fixture
def plugin():
    names = []

    def reg(name, fn, domain="any"):
        register_rule(name, fn, domain)
        names.append(name)
    yield reg
    for n in names:
        unregister_rule(n)


def test_plugin_findings_are_added_and_attributed(node_repo, plugin):
    def max_gpus(ctx):
        gpus = [r for r in ctx.after.components if r.startswith("GPU")]
        ctx.add("info", f"{len(gpus)} accelerators", "counted by a site rule", refs=gpus[:1])
    plugin("count", max_gpus)
    rep = analyze(node_repo, "v1.0", "v1.1")
    f = next(f for f in rep["findings"] if f["title"] == "8 accelerators")
    assert f["confidence_class"] == "rule_based"
    assert {"type": "plugin", "plugin": "count"} in f["evidence"]
    status = next(p for p in rep["rules"]["plugins"] if p["name"] == "count")
    assert status["status"] == "ok" and status["findings"] == [f["id"]]


def test_plugins_cannot_claim_verified(node_repo, plugin):
    plugin("liar", lambda ctx: ctx.add("info", "trust me", "", confidence="verified"))
    rep = analyze(node_repo, "v1.0", "v1.1")
    assert not any(f["title"] == "trust me" for f in rep["findings"])
    failed = next(f for f in rep["findings"] if f["title"] == "Rule plugin liar failed")
    assert "not 'verified'" in failed["detail"]


def test_crashing_plugin_is_disclosed(node_repo, plugin):
    def boom(ctx):
        raise RuntimeError("inventory service down")
    plugin("site", boom)
    rep = analyze(node_repo, "v1.0", "v1.1")
    f = next(f for f in rep["findings"] if f["title"] == "Rule plugin site failed")
    assert f["severity"] == "warning" and "inventory service down" in f["detail"]
    assert next(p for p in rep["rules"]["plugins"] if p["name"] == "site")["status"] == "error"


def test_domain_filter(node_repo, plugin):
    plugin("board-only", lambda ctx: ctx.add("info", "board rule ran", ""), domain="board")
    rep = analyze(node_repo, "v1.0", "v1.1")
    assert not any(f["title"] == "board rule ran" for f in rep["findings"])
    assert rep["rules"]["domain"] == "node"


def test_example_site_rules_via_env(node_repo, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "examples" / "plugins"))
    monkeypatch.setenv("VCM_RULE_PLUGINS", "site_rules:production_policy")
    sys.modules.pop("site_rules", None)
    rep = analyze(node_repo, "v1.0", "v1.1")
    titles = {f["title"] for f in rep["findings"]}
    assert "Driver branch 470 is retired at this site" in titles
    assert not any("above the site's" in t for t in titles)  # H100 NVL is 400 W, under the cap
