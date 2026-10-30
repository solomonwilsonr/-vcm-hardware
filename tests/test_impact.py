import json

from conftest import SAMPLE, _copy_tree
from vcm_ai import CLASSES, NoopProvider, analyze, get_provider, to_markdown
from vcm_ai.engine import parse_uf, parse_volts


def by_class(report, cls):
    return [f for f in report["findings"] if f["confidence_class"] == cls]


def test_every_finding_has_one_valid_class_and_evidence(sample_repo):
    rep = analyze(sample_repo, "v1.0", "v1.1")
    assert rep["findings"]
    for f in rep["findings"]:
        assert f["confidence_class"] in CLASSES
        types = {e["type"] for e in f["evidence"]}
        assert types & {"file", "ref", "commit"}
        assert "commit" in types
    assert set(rep["summary"]["by_class"]) == set(CLASSES)
    assert all(by_class(rep, c) for c in CLASSES)
    json.dumps(rep)  # serialisable


def test_regulator_swap_flags_current_and_thermal(sample_repo):
    rep = analyze(sample_repo, "v1.0", "v1.1")
    rb = {f["title"]: f for f in by_class(rep, "rule_based")}
    assert rb["U1 cannot supply peak load on +3V3"]["severity"] == "critical"
    assert any("thermal" in t for t in rb)
    assert any("Firmware pinned to U2" in t for t in rb)  # domain-neutral wording
    verified = " ".join(f["title"] for f in by_class(rep, "verified"))
    assert "U1 replaced" in verified and "U1 footprint changed" in verified
    retests = {r["name"] for r in rep["required_retests"]}
    assert {"wifi-tx-burst", "power-rail-bringup", "peak-load stress test"} <= retests


def test_speculative_derived_from_rule_findings(sample_repo):
    rep = analyze(sample_repo, "v1.0", "v1.1")
    ids = {f["id"] for f in rep["findings"]}
    spec = [f for f in by_class(rep, "speculative") if "derived_from" in f]
    assert spec and all(set(f["derived_from"]) <= ids for f in spec)


def test_no_change_has_no_rule_findings(sample_repo):
    rep = analyze(sample_repo, "v1.1", "v1.1")
    assert not by_class(rep, "rule_based") and not by_class(rep, "speculative")


def test_markdown_separates_classes(sample_repo):
    md = to_markdown(analyze(sample_repo, "v1.0", "v1.1"))
    i1, i2, i3 = (md.index(f"## {t} (") for t in ("Verified", "Rule-based", "Speculative"))
    assert i1 < i2 < i3
    assert "U1 cannot supply peak load" in md[i2:i3]
    assert "Radio transmit bursts" in md[i3:]
    assert "## Required re-tests" in md


def test_default_provider_is_noop_and_custom_provider_is_speculative(sample_repo):
    assert isinstance(get_provider(), NoopProvider)
    assert NoopProvider().complete("x") is None

    class Fake:
        name = "fake"

        def complete(self, prompt):
            assert "Summarise" in prompt
            return "looks risky"

    rep = analyze(sample_repo, "v1.0", "v1.1", provider=Fake())
    llm = [f for f in rep["findings"] if f["title"].startswith("LLM commentary")]
    assert len(llm) == 1 and llm[0]["confidence_class"] == "speculative"
    assert rep["provider"] == "fake"


def test_pinout_mismatch_detected(sample_repo, tmp_path):
    r = sample_repo
    _copy_tree(SAMPLE / "v1.1", r.root)
    pcb = r.root / "sensor_node.kicad_pcb"
    text = pcb.read_text()
    start = text.index('(footprint "Package_TO_SOT_SMD:SOT-23"')
    end = text.index("(footprint", start + 10)
    blk = text[start:end].replace('(net 2 "+3V3")', "@@").replace(
        '(net 1 "+5V")', '(net 2 "+3V3")').replace("@@", '(net 1 "+5V")')
    pcb.write_text(text[:start] + blk + text[end:])
    r.commit("swap pins", timestamp="2026-03-01T00:00:00Z")
    rep = analyze(r, "v1.1", "HEAD")
    titles = [f["title"] for f in by_class(rep, "rule_based")]
    assert any("VOUT connects to a different net" in t for t in titles)
    assert any(f["title"].startswith("Net ") for f in by_class(rep, "verified"))


def test_esr_rule_when_tantalum_becomes_ceramic(sample_repo):
    r = sample_repo
    _copy_tree(SAMPLE / "v1.0", r.root)
    pcb = r.root / "sensor_node.kicad_pcb"
    pcb.write_text(pcb.read_text().replace("Capacitor_Tantalum_SMD:CP_EIA-3528-21_Kemet-B",
                                           "Capacitor_SMD:C_0805_2012Metric"))
    r.commit("ceramic out cap", timestamp="2026-03-02T00:00:00Z")
    rep = analyze(r, "v1.0", "HEAD")
    assert any("ESR" in f["title"] for f in by_class(rep, "rule_based"))


def test_value_parsers():
    assert parse_uf("22uF") == 22 and parse_uf("100nF") == 0.1 and parse_uf("abc") is None
    assert parse_volts("+3V3") == 3.3 and parse_volts("+5V") == 5 and parse_volts("GND") is None
