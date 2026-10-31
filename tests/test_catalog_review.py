import csv
import io
import json
import shutil
from pathlib import Path

import pytest

from vcm_ai import analyze
from vcm_ai import review as cr
from vcm_ai.catalog import Catalog, CatalogError, unverified_fields
from vcm_ai.engine import trust_figure
from vcm_cli.main import main
from vcm_core import Repository
from vcm_trace import register_device
from vcm_workflow import create_release

NODE = Path(__file__).resolve().parent.parent / "examples" / "gpu-node"
H100 = "NVIDIA H100 NVL 94GB"


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
    create_release(r, "R2", "v1.1")
    for s, rel in (("A", "R1"), ("B", "R1"), ("C", "R2")):
        register_device(r, s, rel)
    return r


def catalog(repo):
    return Catalog.load(project_dir=repo.root, user_dir=repo.root / "nonexistent")


def fill(text, rows):
    """Fill in worksheet rows: rows maps (part, field) -> dict of the columns to set."""
    out = io.StringIO()
    reader = csv.DictReader(io.StringIO(text))
    w = csv.DictWriter(out, fieldnames=reader.fieldnames)
    w.writeheader()
    for row in reader:
        row.update(rows.get((row["part"], row["field"]), {}))
        w.writerow(row)
    return out.getvalue()


OK = {"decision": "ok", "datasheet_source": "vendor datasheet rev B p.4", "checked_by": "dana"}


def test_priority_ranks_by_how_many_units_carry_the_part(fleet):
    rows = cr.priority(fleet, catalog(fleet))
    by = {r["part"]: r for r in rows}
    assert by["NVIDIA A100 PCIe 40GB"]["units"] == 2 and by["NVIDIA A100 PCIe 40GB"][
        "components"] == 16
    assert by[H100]["units"] == 1
    assert by["M393A4K40DB3-CWE"]["units"] == 3 and by["M393A4K40DB3-CWE"]["components"] == 48
    assert [r["units"] for r in rows] == sorted((r["units"] for r in rows), reverse=True)
    assert all(r["units"] > 0 for r in rows)               # parts nobody uses are not listed
    assert all(r["counted_by"] == "units" for r in rows)
    assert not any(r["reviewed"] for r in rows)


def test_priority_without_units_counts_a_revision(tmp_path):
    r = Repository.init(tmp_path / "solo")
    shutil.copytree(NODE / "v1.1", r.root, dirs_exist_ok=True)
    r.commit("v", author="t")
    rows = cr.priority(r, catalog(r), ["HEAD"])
    assert rows and rows[0]["counted_by"] == "revisions"
    assert any(x["part"] == H100 for x in rows)


def test_worksheet_lists_unverified_values_most_used_first(fleet):
    text = cr.worksheet(fleet, catalog(fleet), top=2, type_=None)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == cr.COLUMNS
    assert rows[0]["part"] == "M393A4K40DB3-CWE"            # the most-used part comes first
    assert len({r["part"] for r in rows}) == 2
    assert all(r["decision"] == "" and r["checked_by"] == "" for r in rows)
    only_gpu = list(csv.DictReader(io.StringIO(cr.worksheet(fleet, catalog(fleet),
                                                            type_="gpu"))))
    assert {r["type"] for r in only_gpu} == {"gpu"}


def test_applying_a_filled_sheet_records_who_checked_what(fleet):
    cat = catalog(fleet)
    dimm = "M393A4K40DB3-CWE"
    sheet = cr.worksheet(fleet, cat, type_="dimm")
    filled = fill(sheet, {(dimm, "tdp_w"): OK,
                          (dimm, "capacity_gb"): {**OK, "decision": "correct",
                                                  "corrected_value": "64"}})
    res = cr.apply_worksheet(cat, filled, "2026-10-31T10:00:00Z")
    assert res["confirmed"] == 1 and res["corrected"] == 1 and res["skipped"] >= 1
    part = res["parts"][0]
    assert part["capacity_gb"] == 64.0                      # corrected, and numeric
    assert part["sources"]["tdp_w"] == {"source": "vendor datasheet rev B p.4",
                                        "verified_by": "dana",
                                        "verified_at": "2026-10-31T10:00:00Z"}
    assert part["reviewed"] is False                         # two of four values is not enough
    assert unverified_fields(part) == res["still_unverified"][dimm] and res[
        "now_reviewed"] == []


def test_a_part_becomes_reviewed_only_when_every_value_is_checked(fleet):
    cat = catalog(fleet)
    sheet = cr.worksheet(fleet, cat, type_="psu")
    every = {(r["part"], r["field"]): OK for r in csv.DictReader(io.StringIO(sheet))
             if r["part"] == "PWS-2K04A-1R"}
    res = cr.apply_worksheet(cat, fill(sheet, every), "now")
    assert res["now_reviewed"] == ["PWS-2K04A-1R"] and res["parts"][0]["reviewed"] is True


def test_every_bad_row_is_reported_and_nothing_is_applied(fleet):
    cat = catalog(fleet)
    sheet = cr.worksheet(fleet, cat, type_="gpu", top=1)
    first = next(csv.DictReader(io.StringIO(sheet)))
    p = first["part"]
    fields = [r["field"] for r in csv.DictReader(io.StringIO(sheet))]
    bad = {
        (p, fields[0]): {**OK, "checked_by": "agent:claude"},
        (p, fields[1]): {**OK, "datasheet_source": ""},
        (p, fields[2]): {**OK, "decision": "maybe"},
        (p, fields[3]): {**OK, "decision": "correct"},                    # no corrected_value
        (p, fields[4]): {**OK, "value": "999999"},                        # not what the sheet said
        (p, fields[5]): {**OK, "checked_by": "ai"},
    }
    before = json.dumps(cat.parts, sort_keys=True)
    with pytest.raises(CatalogError) as err:
        cr.apply_worksheet(cat, fill(sheet, {**bad, (p, fields[6]): OK}), "now")
    msg = str(err.value)
    assert msg.startswith("6 problem(s), nothing applied")
    for needle in ("must name the person", "must cite", "not one of ok", "needs corrected_value",
                   "regenerate the worksheet"):
        assert needle in msg
    assert json.dumps(cat.parts, sort_keys=True) == before   # the one good row did not sneak in


def test_unknown_part_field_and_missing_columns(fleet):
    cat = catalog(fleet)
    bad = ("part,field,decision,checked_by,datasheet_source,value\n"
           f"NOPE-123,tdp_w,ok,dana,datasheet p1,1\n{H100},colour,ok,dana,datasheet p1,1\n")
    with pytest.raises(CatalogError, match="(?s)no such part.*not a reviewable field"):
        cr.apply_worksheet(cat, bad, "now")
    with pytest.raises(CatalogError, match="missing column"):
        cr.apply_worksheet(cat, "part,field\nx,y\n", "now")


def test_the_loop_from_worksheet_to_the_next_impact_report(fleet, monkeypatch, capsys):
    monkeypatch.chdir(fleet.root)
    before = analyze(fleet, "v1.0", "v1.1")["summary"]["trust"]
    assert before["catalog_values"] >= 1 and before["catalog_verified"] == 0

    assert main(["-C", str(fleet.root), "catalog", "worksheet", "--type", "gpu",
                 "--out", str(fleet.root.parent / "ws.csv")]) == 0
    sheet = (fleet.root.parent / "ws.csv").read_text()
    mine = {(H100, f): OK for f in ("min_driver", "min_cuda")}
    filled = fleet.root.parent / "done.csv"
    filled.write_text(fill(sheet, mine))
    capsys.readouterr()
    assert main(["-C", str(fleet.root), "catalog", "review", "--from-csv", str(filled)]) == 0
    assert "2 confirmation(s)" in capsys.readouterr().out
    reviews = json.loads((fleet.root / "vcm-catalog" / "reviews.json").read_text())["parts"]
    assert reviews[0]["sources"]["min_driver"]["verified_by"] == "dana"

    after = analyze(fleet, "v1.0", "v1.1")["summary"]["trust"]
    assert after["catalog_verified"] == 2 and after["catalog_values"] == before["catalog_values"]
    assert any(e["action"] == "catalog.review" and e["actor"] == "dana"
               for e in fleet.audit.events())
    assert main(["-C", str(fleet.root), "catalog", "priority", "--type", "gpu"]) == 0
    assert "2/" in capsys.readouterr().out


def test_trust_figure_counts_each_distinct_value_once():
    ev = [{"type": "db", "part": "P", "ref": "G0", "fields": {
        "min_driver": {"from": "catalog", "verified_by": "dana"},
        "tdp_w": {"from": "catalog", "verified_by": ""},
        "pcie_gen": {"from": "node manifest"}}}]
    findings = [{"evidence": ev}, {"evidence": ev}, {"evidence": [{"type": "ref", "ref": "G0"}]}]
    assert trust_figure(findings) == {"catalog_values": 2, "catalog_verified": 1,
                                      "declared_values": 1}


def test_report_and_markdown_carry_the_trust_figure(fleet):
    from vcm_ai import to_markdown
    rep = analyze(fleet, "v1.0", "v1.1")
    assert "(0 checked by a named person)" in to_markdown(rep)
    assert "not every input is cited individually" in to_markdown(rep)
