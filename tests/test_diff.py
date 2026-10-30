from vcm_diff import diff_designs, diff_revisions, render_text
from vcm_parsers import Component, Design


def design(*comps):
    return Design(components={c.ref: c for c in comps})


def comp(ref, value="1k", fp="R_0603", mpn="M", x=0.0, y=0.0, pads=None, **kw):
    return Component(ref=ref, value=value, footprint=fp, mpn=mpn, x=x, y=y, pads=pads or {}, **kw)


def test_added_removed_and_counts():
    d = diff_designs(design(comp("R1"), comp("R2")), design(comp("R1"), comp("R3")))
    assert [x["ref"] for x in d["bom"]["added"]] == ["R3"]
    assert [x["ref"] for x in d["bom"]["removed"]] == ["R2"]
    assert d["bom"]["count_changes"] == []  # same value/footprint count before and after


def test_value_changed_vs_replaced():
    old = design(comp("R1", "1k"), comp("U1", "A", mpn="A"))
    new = design(comp("R1", "2k"), comp("U1", "B", mpn="B"))
    d = diff_designs(old, new)
    assert d["bom"]["value_changed"] == [{"ref": "R1", "old_value": "1k", "new_value": "2k"}]
    assert d["bom"]["replaced"][0]["ref"] == "U1"
    assert "value" in d["bom"]["replaced"][0]["changed_fields"]
    assert d["bom"]["count_changes"]


def test_footprint_and_moved():
    old = design(comp("U1", fp="A:SOT-223", x=1, y=1), comp("R1", x=0, y=0))
    new = design(comp("U1", fp="A:SOT-23", x=1, y=1), comp("R1", x=3, y=4))
    d = diff_designs(old, new)
    assert d["pcb"]["footprint_changes"] == [{"ref": "U1", "old": "A:SOT-223", "new": "A:SOT-23"}]
    assert d["pcb"]["moved"][0]["distance_mm"] == 5.0
    assert d["changed_refs"] == ["R1", "U1"]


def test_net_connectivity_changes():
    old = design(comp("U1", pads={"1": "GND", "2": "A"}), comp("R1", pads={"1": "A"}))
    new = design(comp("U1", pads={"1": "GND", "2": "B"}), comp("R1", pads={"1": "A", "2": "GND"}))
    d = diff_designs(old, new)
    assert d["nets"]["added"] == ["B"] and d["nets"]["removed"] == []
    changed = {c["net"]: c for c in d["nets"]["changed"]}
    assert changed["GND"]["pins_added"] == ["R1.2"]
    assert changed["A"]["pins_removed"] == ["U1.2"]


def test_no_changes_text():
    d = diff_designs(design(comp("R1")), design(comp("R1")))
    assert "No semantic" in render_text(d)


def test_sample_revisions_diff(sample_repo):
    d = diff_revisions(sample_repo, "v1.0", "v1.1")
    assert {x["ref"] for x in d["bom"]["replaced"]} == {"U1", "C2"}
    assert {x["ref"] for x in d["pcb"]["footprint_changes"]} == {"U1", "C2"}
    assert {x["ref"] for x in d["pcb"]["moved"]} == {"C2", "R1"}
    assert d["changed_refs"] == ["C2", "R1", "U1"]
    assert any(m["path"].endswith(".kicad_pcb") for m in d["artifacts"]["modified"])
    text = render_text(d)
    assert "AMS1117-3.3" in text and "MCP1700-3302E/TT" in text
