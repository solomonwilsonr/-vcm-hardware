import pytest

from vcm_graph import build_graph, project_config


def test_graph_nodes_edges(sample_repo):
    g = build_graph(sample_repo, "v1.0")
    st = g.stats()
    assert st["nodes"]["component"] == 12 and "net" in st["nodes"]
    assert {"connects_to", "placed_on", "configured_by", "validated_by"} <= set(st["edges"])
    assert g.node("net:GND")["ground"] is True
    assert g.node("net:+3V3")["ground"] is False
    assert [d for d, _ in g.out("component:U2", "configured_by")] == [
        "firmware:firmware/config.h", "firmware:firmware/main.c"]


def test_affected_by_regulator(sample_repo):
    g = build_graph(sample_repo, "v1.0")
    a = g.affected_by("U1")
    assert set(a["nets"]) == {"GND", "+3V3", "+5V"}
    refs = {c["ref"] for c in a["components"]}
    assert {"U2", "C2", "C1", "J1", "R1"} <= refs
    assert "D1" not in refs  # only shares GND with U1, which is not followed
    assert {f["node"] for f in a["firmware"]} == {"firmware:firmware/main.c", "firmware:firmware/config.h"}
    assert {t["node"] for t in a["tests"]} == {"test:power-rail-bringup", "test:wifi-tx-burst"}
    assert "Package_TO_SOT_SMD:SOT-223-3_TabPin2" in " ".join(a["footprints"])
    for c in a["components"]:
        assert c["path"][0] == "component:U1" and c["path"][-1] == f"component:{c['ref']}"


def test_affected_leaf_component_and_unknown(sample_repo):
    g = build_graph(sample_repo, "v1.0")
    a = g.affected_by("D1")
    assert {c["ref"] for c in a["components"]} == {"R2"}
    with pytest.raises(KeyError):
        g.affected_by("Z99")


def test_graph_persisted_per_revision(sample_repo):
    g1 = build_graph(sample_repo, "v1.0")
    g2 = build_graph(sample_repo, "v1.1")
    assert g1.node("component:U1")["value"] == "AMS1117-3.3"
    assert g2.node("component:U1")["value"] == "MCP1700-3302E/TT"
    assert (sample_repo.dir / "graph.db").exists()
    assert g1.node("component:U1")["value"] == "AMS1117-3.3"  # still isolated by revision


def test_project_config(sample_repo):
    assert project_config(sample_repo, "v1.0")["rails"]["+3V3"] == 3.3
