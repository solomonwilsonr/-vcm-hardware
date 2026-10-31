"""The impact report's published schema, plus randomised property tests of the core invariants.

The property tests use the standard library's ``random`` with fixed seeds, so they explore many
inputs while staying deterministic and dependency-free.
"""
import json
import random
import re
import shutil
from pathlib import Path

import pytest

from vcm_ai import analyze
from vcm_core import AuditLog, ObjectStore, canonical_json, ed25519
from vcm_diff import compare_drift, diff_designs
from vcm_parsers import Component, Design
from vcm_trace.fleet import _version

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs" / "schemas" / "impact-report.schema.json").read_text())
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
         "boolean": bool}


def validate(value, schema, path="$", root=SCHEMA):
    """The subset of JSON Schema the published schema uses; raises AssertionError on mismatch."""
    if "$ref" in schema:
        schema = root["$defs"][schema["$ref"].rsplit("/", 1)[-1]]
    if "const" in schema:
        assert value == schema["const"], f"{path}: {value!r} != {schema['const']!r}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"
    if "type" in schema:
        t = TYPES[schema["type"]]
        assert isinstance(value, t) and not (schema["type"] in ("integer", "number")
                                             and isinstance(value, bool)), \
            f"{path}: expected {schema['type']}, got {type(value).__name__}"
    if isinstance(value, str):
        if "pattern" in schema:
            assert re.search(schema["pattern"], value), f"{path}: {value!r} !~ {schema['pattern']}"
        assert len(value) >= schema.get("minLength", 0), f"{path}: too short"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        assert value >= schema.get("minimum", value), f"{path}: below minimum"
        assert value <= schema.get("maximum", value), f"{path}: above maximum"
    if isinstance(value, dict):
        for key in schema.get("required", []):
            assert key in value, f"{path}: missing {key}"
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                validate(value[key], sub, f"{path}.{key}", root)
    if isinstance(value, list):
        assert len(value) >= schema.get("minItems", 0), f"{path}: too few items"
        for i, item in enumerate(value):
            if "items" in schema:
                validate(item, schema["items"], f"{path}[{i}]", root)


def _node_repo(tmp_path):
    from vcm_core import Repository
    r = Repository.init(tmp_path / "n")
    for v in ("v1.0", "v1.1"):
        for p in list(r.root.iterdir()):
            if p.name != ".vcm":
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        shutil.copytree(ROOT / "examples" / "gpu-node" / v, r.root, dirs_exist_ok=True)
        r.commit(v, author="t")
        r.tag(v)
    return r


def test_node_report_matches_published_schema(tmp_path):
    validate(analyze(_node_repo(tmp_path), "v1.0", "v1.1"), SCHEMA)


def test_board_report_matches_published_schema(sample_repo):
    validate(analyze(sample_repo, "v1.0", "v1.1"), SCHEMA)


def test_validator_catches_violations(tmp_path):
    rep = analyze(_node_repo(tmp_path), "v1.0", "v1.1")
    rep["findings"][0]["confidence_class"] = "probably"
    with pytest.raises(AssertionError, match="confidence_class"):
        validate(rep, SCHEMA)


# ---- properties ------------------------------------------------------------------------
def _rand_obj(rng, depth=0):
    kind = rng.choice(["int", "str", "list", "dict"] if depth < 3 else ["int", "str"])
    if kind == "int":
        return rng.randint(-10**6, 10**6)
    if kind == "str":
        return "".join(rng.choice("abcxyz_-é0") for _ in range(rng.randint(0, 6)))
    if kind == "list":
        return [_rand_obj(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {f"k{rng.randint(0, 20)}": _rand_obj(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def test_canonical_json_ignores_key_order():
    rng = random.Random(1)
    for _ in range(200):
        obj = _rand_obj(rng)
        if isinstance(obj, dict):
            shuffled = dict(rng.sample(list(obj.items()), len(obj)))
            assert canonical_json(obj) == canonical_json(shuffled)
        assert json.loads(canonical_json(obj)) == obj


def test_object_store_roundtrip_and_dedup(tmp_path):
    rng = random.Random(2)
    store = ObjectStore(tmp_path / "o")
    seen = {}
    for _ in range(100):
        data = rng.randbytes(rng.randint(0, 3000))
        d = store.put_bytes(data)
        assert store.read(d) == data and store.verify(d)
        seen[d] = data
    assert sorted(store.iter_digests()) == sorted(seen)


def _rand_design(rng, n=None) -> Design:
    d = Design()
    for i in range(n if n is not None else rng.randint(0, 12)):
        ref = f"{rng.choice(['GPU', 'CPU', 'DIMM', 'NIC'])}{i}"
        d.components[ref] = Component(
            ref=ref, value=rng.choice(["A", "B", "C"]), mpn=rng.choice(["", "P1", "P2"]),
            lib_id=rng.choice(["gpu", "cpu", "dimm", "nic"]),
            pads={"power": "PSU_BANK", "pcie": rng.choice(["ROOT0", "ROOT1"])})
    return d


def test_diff_of_identical_designs_is_empty():
    rng = random.Random(3)
    for _ in range(100):
        d = _rand_design(rng)
        assert diff_designs(d, d)["changed_refs"] == []


def test_diff_is_symmetric_in_what_changed():
    rng = random.Random(4)
    for _ in range(100):
        a, b = _rand_design(rng), _rand_design(rng)
        fwd, back = diff_designs(a, b), diff_designs(b, a)
        assert fwd["changed_refs"] == back["changed_refs"]
        assert ({x["ref"] for x in fwd["bom"]["added"]}
                == {x["ref"] for x in back["bom"]["removed"]})


def test_a_machine_that_matches_its_manifest_never_drifts():
    rng = random.Random(5)
    for _ in range(100):
        d = _rand_design(rng)
        obs = {"covers": ["gpu", "cpu", "dimm", "nic"], "sources": ["test"], "firmware": {},
               "components": [{"type": c.lib_id, "model": c.value, "mpn": c.mpn, "serial": "",
                               "slot": ""} for c in d.components.values()]}
        rng.shuffle(obs["components"])
        res = compare_drift(d, {}, obs)
        assert res["status"] == "in_sync", res["findings"]


def test_removing_any_observed_part_is_detected():
    rng = random.Random(6)
    for _ in range(50):
        d = _rand_design(rng, n=rng.randint(1, 10))
        comps = [{"type": c.lib_id, "model": c.value, "mpn": c.mpn, "serial": "", "slot": ""}
                 for c in d.components.values()]
        comps.pop(rng.randrange(len(comps)))
        res = compare_drift(d, {}, {"covers": ["gpu", "cpu", "dimm", "nic"], "components": comps,
                                    "sources": [], "firmware": {}})
        assert sum(f["kind"] == "missing" for f in res["findings"]) == 1


def test_signatures_roundtrip_and_reject_any_bit_flip():
    rng = random.Random(7)
    for _ in range(8):
        sk = rng.randbytes(32)
        msg = rng.randbytes(rng.randint(0, 64))
        sig = ed25519.sign(sk, msg)
        pub = ed25519.public_key(sk)
        assert ed25519.verify(pub, msg, sig)
        bad = bytearray(sig)
        bad[rng.randrange(64)] ^= 1 << rng.randrange(8)
        assert not ed25519.verify(pub, msg, bytes(bad))


def test_any_audit_edit_breaks_the_chain(tmp_path):
    rng = random.Random(8)
    for trial in range(10):
        log = AuditLog(tmp_path / f"a{trial}")
        for i in range(rng.randint(2, 8)):
            log.append(rng.choice(["commit", "tag", "release.create"]), f"s{i}", {"i": i}, "x")
        assert log.verify()["ok"]
        lines = log.path.read_text().splitlines()
        i = rng.randrange(len(lines))
        ev = json.loads(lines[i])
        ev[rng.choice(["subject", "actor", "action"])] += "!"
        lines[i] = json.dumps(ev)
        log.path.write_text("\n".join(lines) + "\n")
        res = log.verify()
        assert not res["ok"] and res["broken_at"] == i + 1


def test_version_ordering_matches_numeric_tuples():
    rng = random.Random(9)
    for _ in range(300):
        a = tuple(rng.randint(0, 600) for _ in range(rng.randint(1, 3)))
        b = tuple(rng.randint(0, 600) for _ in range(rng.randint(1, 3)))
        sa, sb = ".".join(map(str, a)), ".".join(map(str, b))
        assert (_version(sa) < _version(sb)) == (a < b)
