import pytest

from vcm_parsers import SexprError, build_design, parse_bom, parse_pcb, parse_schematic, parse_sexpr
from vcm_parsers.sexpr import dump


def test_sexpr_basic_and_roundtrip():
    tree = parse_sexpr('(a (b "x y" 1.5) (c "q\\"uote") d)')
    assert tree == ["a", ["b", "x y", "1.5"], ["c", 'q"uote'], "d"]
    assert parse_sexpr(dump(tree)) == tree


@pytest.mark.parametrize("bad", ["(a (b)", "a)", "(a) (b)", '(a "unterminated)', ""])
def test_sexpr_errors(bad):
    with pytest.raises(SexprError):
        parse_sexpr(bad)


def test_sexpr_deep_nesting_no_recursion_error():
    depth = 5000
    parse_sexpr("(" * depth + ")" * depth)


PCB = """(kicad_pcb (version 20240108)
 (net 0 "") (net 1 "GND") (net 2 "+3V3")
 (footprint "Pkg:SOT-23" (layer "F.Cu") (at 10 20 90)
   (property "Reference" "U1") (property "Value" "LDO")
   (pad "1" smd rect (at 0 0) (net 1 "GND")) (pad "2" smd rect (at 1 0) (net 2 "+3V3"))
   (pad "3" smd rect (at 2 0) (net 0 "")))
 (module "Old:R" (layer "F.Cu") (at 1 2)
   (fp_text reference "R1" (at 0 0)) (fp_text value "10k" (at 0 0))
   (pad "1" smd rect (at 0 0) (net "GND"))))"""


def test_parse_pcb_components_nets_and_old_syntax():
    comps, declared, dups = parse_pcb(PCB)
    by = {c.ref: c for c in comps}
    assert declared == {"GND", "+3V3"} and not dups
    u1 = by["U1"]
    assert (u1.x, u1.y, u1.rotation, u1.layer) == (10, 20, 90, "F.Cu")
    assert u1.pads == {"1": "GND", "2": "+3V3", "3": ""}
    assert by["R1"].value == "10k" and by["R1"].pads["1"] == "GND"


def test_parse_schematic_skips_lib_symbols_and_power():
    sch = """(kicad_sch (lib_symbols (symbol "Device:R" (property "Reference" "R")))
      (symbol (lib_id "Device:R") (at 0 0 0) (property "Reference" "R?") (property "Value" "1k")
        (property "Footprint" "Resistor_SMD:R_0603") (instances (project "p" (path "/x" (reference "R7") (unit 1)))))
      (symbol (lib_id "power:GND") (property "Reference" "#PWR01") (property "Value" "GND"))
      (symbol (lib_id "Device:R") (property "Reference" "R7") (property "Value" "dup")))"""
    comps, dups = parse_schematic(sch)
    assert [c.ref for c in comps] == ["R7"] and comps[0].value == "1k"
    assert dups == ["R7"]


def test_parse_bom_groups_aliases_and_qty_mismatch():
    csv_text = "Designator,Comment,Package,Quantity,MPN\n\"C1, C2\",100nF,C_0603,3,X\nR1,10k,R_0603,1,Y\nR1,dup,R_0603,1,Z\n"
    comps, dups, mism = parse_bom(csv_text)
    assert [c.ref for c in comps] == ["C1", "C2", "R1"]
    assert dups == ["R1"] and mism == [{"refs": ["C1", "C2"], "qty": 3}]
    assert comps[0].footprint == "C_0603" and comps[0].mpn == "X"


def test_build_design_merges_sources(sample_repo):
    from vcm_parsers import design_from_revision
    d = design_from_revision(sample_repo, "v1.0")
    assert len(d.components) == 12 and set(d.sources) == {"bom", "pcb", "schematic"}
    u1 = d.components["U1"]
    assert u1.value == "AMS1117-3.3" and u1.pads["3"] == "+5V" and u1.mpn == "AMS1117-3.3"
    assert d.nets["+5V"] == ["C1.1", "J1.1", "U1.3"]
    assert not d.duplicates


def test_non_bom_csv_ignored():
    d = build_design({"data.csv": b"a,b\n1,2\n"})
    assert not d.components
