from vcm_parsers import Component, Design
from vcm_validation import (
    check_bom_text,
    check_components,
    check_nets,
    compare_validation,
    run_kicad_checks,
    validate_revision,
)


def codes(issues):
    return sorted(i["code"] for i in issues)


def test_component_checks():
    d = Design(components={"R1": Component("R1", value="", footprint="")},
               duplicates=[{"ref": "C1", "source": "bom.csv"}])
    assert codes(check_components(d)) == ["DUPLICATE_REF", "MISSING_FOOTPRINT", "MISSING_VALUE"]


def test_source_mismatch():
    c = Component("R1", value="1k", footprint="F", by_source={
        "schematic": {"value": "1k", "footprint": "F"}, "bom": {"value": "2k", "footprint": "F"}})
    d = Design(components={"R1": c}, sources={"schematic": ["a"], "bom": ["b"]})
    assert "SOURCE_MISMATCH" in codes(check_components(d))


def test_net_checks():
    d = Design(components={
        "R1": Component("R1", pads={"1": "A", "2": "GND", "3": ""}),
        "R2": Component("R2", pads={"1": "GND"})}, declared_nets={"A", "GND", "ORPHAN"})
    got = codes(check_nets(d))
    assert got == ["EMPTY_NET", "UNCONNECTED_NET", "UNCONNECTED_PAD"]


def test_bom_text_checks():
    text = "Reference,Value,Footprint,Qty\n\"R1,R2\",1k,,3\nR1,2k,F,1\n"
    assert codes(check_bom_text(text)) == ["DUPLICATE_REF", "MISSING_FOOTPRINT", "MISSING_FOOTPRINT", "QTY_MISMATCH"]


def test_sample_revisions_pass_and_evidence_versioned(sample_repo):
    r1 = validate_revision(sample_repo, "v1.0")
    assert r1["status"] == "pass" and r1["counts"]["error"] == 0
    validate_revision(sample_repo, "v1.0")
    assert len(sample_repo.evidence_history("v1.0", "validation")) == 2
    assert sample_repo.latest_evidence("v1.0", "validation")["status"] == "pass"


def test_compare_detects_regression(repo):
    root = repo.root
    (root / "bom.csv").write_text("Reference,Value,Footprint\nR1,1k,R_0603\n")
    a = repo.commit("ok")
    (root / "bom.csv").write_text("Reference,Value,Footprint\nR1,1k,R_0603\nR2,,\n")
    b = repo.commit("bad")
    cmp = compare_validation(repo, a.id, b.id)
    assert cmp["regression"] and cmp["status"] == {"from": "pass", "to": "fail"}
    assert {i["code"] for i in cmp["new_issues"]} == {"MISSING_VALUE", "MISSING_FOOTPRINT"}
    back = compare_validation(repo, b.id, a.id)
    assert not back["regression"] and len(back["resolved_issues"]) == 2


def test_kicad_cli_skips_gracefully(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _n: None)
    assert run_kicad_checks({"a.kicad_pcb": b""})["status"] == "skipped"
