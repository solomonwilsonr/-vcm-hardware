"""Semantic part search on Appwrite VectorsDB, exercised offline.

The contract: the vector search produces a *shortlist*, and every entry on it is then judged
against the deterministic catalog facts. A candidate that would not fit must say so, and must never
be presented as a drop-in.
"""
import pytest

from fake_appwrite import FakeEmbeddings, FakeVectorsDB
from vcm_ai.catalog import Catalog
from vcm_ai.semantic import (
    COLLECTION,
    VECTOR_FIELD,
    AppwriteConfigError,
    SemanticCatalog,
    compatibility,
    part_text,
    replacements_for,
)
from vcm_graph.backends.appwrite_backend import AppwriteConfig

CFG = AppwriteConfig(endpoint="http://x", project="p", api_key="k", database="vcm")


@pytest.fixture(params=[False, True], ids=["plain-dicts", "sdk-models"])
def sem(request):
    s = SemanticCatalog(CFG, vectors=FakeVectorsDB(models=request.param),
                        embeddings=FakeEmbeddings(models=request.param))
    s.provision()
    s.index(Catalog.load())
    return s


def test_part_text_is_deterministic_and_descriptive():
    part = {"match": ["NVIDIA H100 NVL", "900-21010"], "type": "gpu", "tdp_w": 400,
            "power_connector": "12VHPWR", "slot_width": 2}
    text = part_text(part)
    assert text == part_text(dict(part))
    for fragment in ("NVIDIA H100 NVL", "gpu", "400 W TDP", "12VHPWR power connector", "2-slot"):
        assert fragment in text


def test_provision_creates_collection_with_the_models_dimension():
    v, e = FakeVectorsDB(), FakeEmbeddings(dim=64)
    done = SemanticCatalog(CFG, vectors=v, embeddings=e).provision()
    assert done["dimension"] == 64
    assert v.collections[COLLECTION]["dimension"] == 64
    assert v.indexes[COLLECTION]["parts_cosine"][0] == "hnsw_cosine"


def test_provision_is_idempotent():
    v, e = FakeVectorsDB(), FakeEmbeddings()
    s = SemanticCatalog(CFG, vectors=v, embeddings=e)
    s.provision()
    again = s.provision()
    assert again["created"] == [] and again["existed"]


def test_index_embeds_every_catalog_part(sem):
    cat = Catalog.load()
    assert sem.index(cat)["indexed"] == len(cat.parts)
    stored = sem.vectors.documents[COLLECTION]
    assert len(stored) == len(cat.parts)
    assert all(len(d[VECTOR_FIELD]) == 64 for d in stored.values())


def test_search_ranks_by_meaning_not_prefix(sem):
    """The question a prefix match cannot answer: no part name appears in the query."""
    hits = sem.similar("high power dual-slot accelerator for training", limit=5)
    assert hits and all(h["type"] == "gpu" for h in hits[:3])
    assert VECTOR_FIELD not in hits[0]          # vectors are not echoed back to callers


def test_search_can_be_restricted_to_a_part_type(sem):
    hits = sem.similar("fast network adapter", limit=5, type_="nic")
    assert hits and {h["type"] for h in hits} == {"nic"}


def test_replacements_shortlist_excludes_the_installed_part(sem):
    out = replacements_for(sem, mpn="NVIDIA A100 PCIe 40GB", limit=5)
    assert out["installed"] == "NVIDIA A100 PCIe 40GB"
    assert all(c["mpn"] != "NVIDIA A100 PCIe 40GB" for c in out["candidates"])
    assert out["candidates"] and out["caveat"]


def test_replacements_are_checked_against_catalog_facts(sem):
    out = replacements_for(sem, mpn="NVIDIA A100 PCIe 40GB", limit=8)
    for cand in out["candidates"]:
        assert cand["verdict"] in ("drop_in", "check", "incompatible")
        if cand["verdict"] != "drop_in":
            assert cand["blockers"] or cand["checks"]


def test_unknown_part_is_refused_rather_than_guessed(sem):
    with pytest.raises(AppwriteConfigError, match="no catalog entry"):
        replacements_for(sem, mpn="DEFINITELY-NOT-A-REAL-PART-9999")


# ---- the checking layer, independent of any vector search ----------
def test_connector_change_is_flagged():
    a100 = {"type": "gpu", "tdp_w": 250, "power_connector": "8-pin EPS", "slot_width": 2}
    h100 = {"type": "gpu", "tdp_w": 400, "power_connector": "12VHPWR", "slot_width": 2}
    res = compatibility(a100, h100)
    assert res["verdict"] == "check"
    assert any("12VHPWR" in c for c in res["checks"])
    assert any("400 W against 250 W" in c for c in res["checks"])


def test_a_different_kind_of_part_is_incompatible():
    res = compatibility({"type": "gpu"}, {"type": "nic"})
    assert res["verdict"] == "incompatible" and res["blockers"]


def test_identical_properties_are_a_drop_in():
    part = {"type": "gpu", "tdp_w": 300, "power_connector": "12VHPWR", "slot_width": 2}
    assert compatibility(part, dict(part))["verdict"] == "drop_in"


def test_a_higher_driver_floor_is_a_check_not_a_drop_in():
    res = compatibility({"type": "gpu", "min_driver": "450.80.02"},
                        {"type": "gpu", "min_driver": "525.60.13"})
    assert res["verdict"] == "check"
    assert any("525.60.13" in c for c in res["checks"])


# ---- CLI wiring -----------------------------------------------------
@pytest.fixture
def wired(monkeypatch):
    """Point the CLI's SemanticCatalog at the fakes, with credentials it will accept."""
    v, e = FakeVectorsDB(), FakeEmbeddings()
    monkeypatch.setattr("vcm_ai.semantic.services", lambda cfg: (v, e))
    for var, val in (("VCM_APPWRITE_ENDPOINT", "http://x"), ("VCM_APPWRITE_PROJECT", "p"),
                     ("VCM_APPWRITE_API_KEY", "k"), ("VCM_APPWRITE_DATABASE", "vcm")):
        monkeypatch.setenv(var, val)
    return v, e


def test_cli_provision_index_and_search(wired, capsys):
    from vcm_cli.main import main
    assert main(["catalog", "provision"]) == 0
    assert "Vector collection ready" in capsys.readouterr().out

    assert main(["catalog", "index"]) == 0
    assert "Indexed 90 parts" in capsys.readouterr().out

    assert main(["catalog", "similar", "dual-slot training accelerator", "--type", "gpu"]) == 0
    assert "NVIDIA" in capsys.readouterr().out


def test_cli_replace_reports_a_verdict_per_candidate(wired, capsys):
    from vcm_cli.main import main
    main(["catalog", "provision"])
    main(["catalog", "index"])
    capsys.readouterr()
    assert main(["catalog", "replace", "NVIDIA A100 PCIe 40GB"]) == 0
    out = capsys.readouterr().out
    assert "Replacements for NVIDIA A100 PCIe 40GB" in out
    assert any(v in out for v in ("DROP_IN", "CHECK", "INCOMPATIBLE"))
    assert "shortlist ranked by meaning" in out
