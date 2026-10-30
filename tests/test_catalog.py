"""The component catalog, its layering, and the disclosure that keeps the engine honest.

The behaviour pinned down here is the one the tool cannot get wrong: when a part is unknown, the
report must say so. Silence is indistinguishable from safety, and an engineer reading a clean
report has no other way to tell the difference.
"""
import json

import pytest

from vcm_ai import analyze
from vcm_ai.catalog import Catalog, CatalogError, read_file
from vcm_parsers import design_from_revision


def _find(report, substring):
    return [f for f in report["findings"] if substring in f["title"]]


def test_bundled_catalog_layers_demo_entries_over_starter():
    cat = Catalog.bundled()
    desc = cat.describe()
    assert desc["parts"] > 80
    assert set(desc["layers"]) == {"bundled:catalog-starter.json",
                                   "bundled:catalog-datacenter.json",
                                   "bundled:components.json"}
    # the curated demo entry wins over the starter entry for the same part
    assert cat.find("MCP1700-3302E/TT")["_origin"] == "bundled:components.json"


def test_longest_prefix_wins_so_specific_beats_generic():
    cat = Catalog([
        {"match": ["LM1117"], "type": "ldo", "vout": 1.2, "iout_max_ma": 800, "source": "",
         "sources": {}, "reviewed": False, "_origin": "t"},
        {"match": ["LM1117-3.3"], "type": "ldo", "vout": 3.3, "iout_max_ma": 800, "source": "",
         "sources": {}, "reviewed": False, "_origin": "t"},
    ])
    assert cat.find("LM1117-3.3")["vout"] == 3.3
    assert cat.find("LM1117-1.2")["vout"] == 1.2    # falls back to the generic entry


def test_project_catalog_overrides_bundled_values(tmp_path):
    folder = tmp_path / "vcm-catalog"
    folder.mkdir()
    (folder / "ours.json").write_text(json.dumps({"parts": [
        {"match": ["MCP1700-3302"], "type": "ldo", "vout": 3.3, "iout_max_ma": 999,
         "source": "our bench measurement", "reviewed": True}]}))
    cat = Catalog.load(project_dir=tmp_path, user_dir=tmp_path / "nope")
    part = cat.find("MCP1700-3302E/TT")
    assert part["iout_max_ma"] == 999 and part["reviewed"] is True


def test_csv_import_round_trip(tmp_path):
    csv = tmp_path / "parts.csv"
    csv.write_text("match,type,vout,iout_max_ma,source\n"
                   "XYZ123|XYZ,ldo,3.3,450,XYZ datasheet\n")
    parts = read_file(csv)
    assert parts[0]["match"] == ["XYZ123", "XYZ"]
    assert parts[0]["iout_max_ma"] == 450.0      # coerced to a number
    assert parts[0]["reviewed"] is False         # imports are unreviewed until a human says so
    assert Catalog(parts).find("XYZ123ABC")["type"] == "ldo"


def test_malformed_catalog_entries_are_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"parts": [{"type": "ldo"}]}))
    with pytest.raises(CatalogError, match="match"):
        read_file(bad)
    worse = tmp_path / "worse.json"
    worse.write_text(json.dumps({"parts": [{"match": ["X"], "iout_max_ma": "lots"}]}))
    with pytest.raises(CatalogError, match="not a number"):
        read_file(worse)


def test_coverage_separates_unknown_parts_from_passives(sample_repo):
    design = design_from_revision(sample_repo, sample_repo.get_revision("v1.0"))
    cov = Catalog.bundled().coverage(design)
    assert "U1" in cov["known"] and "U2" in cov["known"]
    assert all(r[0] in "CRDL" for r in cov["passive"])   # passives need no catalog entry
    assert cov["ratio"] == pytest.approx(len(cov["known"]) / cov["considered"])


def test_unknown_part_is_disclosed_not_silently_skipped(sample_repo):
    """The regression that matters: an unknown part must never read as a clean pass."""
    empty = Catalog([])      # a catalog that knows nothing at all
    rep = analyze(sample_repo, "v1.0", "v1.1", catalog=empty)

    assert not [f for f in rep["findings"] if f["confidence_class"] == "rule_based"]
    disclosures = _find(rep, "is not in the component catalog")
    assert disclosures, "an unknown part produced no disclosure"
    d = disclosures[0]
    assert d["confidence_class"] == "verified"   # a fact about the catalog, not a heuristic
    assert d["severity"] == "warning"
    assert "absence of knowledge" in d["detail"]

    summary = _find(rep, "Catalog covered")[0]
    assert summary["detail"].startswith("Known: none")
    assert rep["summary"]["catalog_coverage"] == 0.0


def test_known_part_reports_full_coverage_and_fires_rules(sample_repo):
    rep = analyze(sample_repo, "v1.0", "v1.1")
    assert rep["summary"]["catalog_coverage"] == 1.0
    assert not _find(rep, "is not in the component catalog")
    assert [f for f in rep["findings"] if f["confidence_class"] == "rule_based"]
    assert _find(rep, "Catalog covered")[0]["severity"] == "info"


def test_rule_findings_carry_catalog_provenance(sample_repo):
    rep = analyze(sample_repo, "v1.0", "v1.1")
    rule = [f for f in rep["findings"] if f["confidence_class"] == "rule_based"][0]
    db_ev = [e for e in rule["evidence"] if e["type"] == "db"]
    assert db_ev, "a rule-based finding must cite the catalog entry it leaned on"
    assert "reviewed" in db_ev[0] and "source" in db_ev[0]


def test_unreviewed_values_are_flagged_in_the_coverage_finding(sample_repo):
    rep = analyze(sample_repo, "v1.0", "v1.1")
    detail = _find(rep, "Catalog covered")[0]["detail"]
    assert "not been checked against a datasheet" in detail
    assert rep["catalog"]["reviewed"] == 0       # nothing bundled is human-reviewed yet


def test_declared_loads_are_not_counted_as_catalog_gaps(sample_repo):
    """A component whose load vcm.json states directly is not a hole in the catalog."""
    design = design_from_revision(sample_repo, sample_repo.get_revision("v1.1"))
    cat = Catalog.bundled()
    bare = cat.coverage(design)
    with_decl = cat.coverage(design, declared={"J2"})
    assert "J2" in bare["unknown"]
    assert "J2" in with_decl["declared"] and "J2" not in with_decl["unknown"]
    assert with_decl["ratio"] > bare["ratio"]

    rep = analyze(sample_repo, "v1.0", "v1.1")
    assert "Declared directly in vcm.json" in _find(rep, "Catalog covered")[0]["detail"]


def test_disclosure_names_what_was_skipped_and_what_still_ran(sample_repo):
    """The disclosure must not claim more ignorance than there is.

    On a node, rules fed by manifest-declared values keep running even when the catalog has no
    entry, so saying "no rules were applied" would be false.
    """
    rep = analyze(sample_repo, "v1.0", "v1.1", catalog=Catalog([]))
    d = _find(rep, "is not in the component catalog")[0]["detail"]
    assert "all of which read the catalog" in d
    assert "absence of knowledge" in d
    assert "Rules fed by values declared" not in d      # board domain: nothing else ran


def test_unknown_parts_are_grouped_by_what_they_are(sample_repo):
    rep = analyze(sample_repo, "v1.0", "v1.1", catalog=Catalog([]))
    disclosures = _find(rep, "is not in the component catalog")
    # one finding per distinct unknown part, not one per reference designator
    assert len(disclosures) == len({t["title"] for t in disclosures})
    for f in disclosures:
        assert "Affected:" in f["detail"]
