import json

import pytest

from vcm_cli.main import main
from vcm_core import VcmError, ed25519, signing
from vcm_trace import passport, register_device
from vcm_workflow import create_release, sign_release, verify_release

# RFC 8032 section 7.1, test vectors 1 and 2.
VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b4"
     "6bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d"
     "8c387b2eaeb4302aeeb00d291612bb0c00"),
]


@pytest.fixture(autouse=True)
def keydir(tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))


@pytest.mark.parametrize("sk,pk,msg,sig", VECTORS)
def test_ed25519_matches_rfc8032(sk, pk, msg, sig):
    seed, m = bytes.fromhex(sk), bytes.fromhex(msg)
    assert ed25519.public_key(seed).hex() == pk
    assert ed25519.sign(seed, m).hex() == sig
    assert ed25519.verify(bytes.fromhex(pk), m, bytes.fromhex(sig))
    assert not ed25519.verify(bytes.fromhex(pk), m + b"x", bytes.fromhex(sig))


def test_ed25519_rejects_malformed_input():
    assert not ed25519.verify(b"short", b"m", b"\0" * 64)
    assert not ed25519.verify(bytes.fromhex(VECTORS[0][1]), b"m", b"\xff" * 64)


def _signed(repo, name="R1", key="alice"):
    pub = signing.generate_key(key)
    create_release(repo, name, "v1.1")
    sign_release(repo, name, key)
    return pub


def test_signed_release_needs_the_key_to_be_trusted(sample_repo):
    pub = _signed(sample_repo)
    res = verify_release(sample_repo, "R1")
    assert not res["ok"] and "not in trustdb.json" in res["problems"][0]["detail"]
    signing.trust_key(sample_repo, "alice", pub)
    res = verify_release(sample_repo, "R1", require_signed=True)
    assert res["ok"] and res["signed_by"] == ["alice"]


def test_unsigned_release_passes_unless_signature_required(sample_repo):
    create_release(sample_repo, "R1", "v1.1")
    assert verify_release(sample_repo, "R1")["ok"]
    res = verify_release(sample_repo, "R1", require_signed=True)
    assert not res["ok"] and res["problems"][0]["check"] == "signature"


def test_forged_manifest_fails_even_if_the_hash_is_recomputed(sample_repo, tmp_path):
    """The point of signing: rewriting manifest + hash no longer forges a release."""
    from vcm_workflow.release import _manifest_hash

    signing.trust_key(sample_repo, "alice", _signed(sample_repo))
    m = sample_repo.load_record("releases", "R1")
    m["revision_message"] = "forged"
    m["manifest_sha256"] = _manifest_hash(m)  # attacker fixes up the self-hash
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps(m))
    res = verify_release(sample_repo, str(forged))
    assert not res["ok"]
    assert {c["check"] for c in res["problems"]} == {"signature:alice"}
    assert "does not match" in res["problems"][0]["detail"]


def test_signature_cannot_borrow_another_keys_name(sample_repo):
    signing.generate_key("mallory")
    signing.trust_key(sample_repo, "alice", _signed(sample_repo, key="alice"))
    m = sample_repo.load_record("releases", "R1")
    sig = signing.make_signature("mallory", m["manifest_sha256"])
    m["signatures"] = [{**sig, "key_id": "alice"}]
    sample_repo.save_record("releases", "R1", m)
    res = verify_release(sample_repo, "R1")
    assert not res["ok"] and "other than the one trusted" in res["problems"][0]["detail"]


def test_trust_refuses_a_silent_key_swap(sample_repo):
    signing.trust_key(sample_repo, "alice", "aa" * 32)
    signing.trust_key(sample_repo, "alice", "aa" * 32)  # idempotent
    with pytest.raises(VcmError, match="different key"):
        signing.trust_key(sample_repo, "alice", "bb" * 32)
    with pytest.raises(VcmError):
        signing.trust_key(sample_repo, "bob", "nothex")


def test_generate_key_never_overwrites_and_is_private(tmp_path):
    signing.generate_key("k")
    assert (signing.key_dir() / "k.key").stat().st_mode & 0o777 == 0o600
    with pytest.raises(VcmError, match="already exists"):
        signing.generate_key("k")
    with pytest.raises(VcmError):
        signing.generate_key("../escape")


def test_cannot_sign_a_tampered_record(sample_repo):
    signing.generate_key("alice")
    create_release(sample_repo, "R1", "v1.1")
    m = sample_repo.load_record("releases", "R1")
    m["name"] = "R1"
    m["revision_message"] = "edited"
    sample_repo.save_record("releases", "R1", m)
    with pytest.raises(VcmError, match="refusing to sign"):
        sign_release(sample_repo, "R1", "alice")


def test_passport_reports_signature_trust(sample_repo):
    pub = _signed(sample_repo)
    register_device(sample_repo, "SN1", "R1")
    sigs = passport(sample_repo, "SN1")["release"]["signatures"]
    assert sigs[0]["key_id"] == "alice" and sigs[0]["trusted"] is False
    signing.trust_key(sample_repo, "alice", pub)
    assert passport(sample_repo, "SN1")["release"]["signatures"][0]["trusted"] is True


def test_cli_key_sign_verify_flow(sample_repo, capsys):
    r = ["--repo", str(sample_repo.root)]
    assert main([*r, "key", "gen", "alice"]) == 0
    assert main([*r, "release", "create", "R1", "--rev", "v1.1"]) == 0
    assert main([*r, "release", "sign", "R1", "--key", "alice"]) == 0
    assert "not in trustdb.json" in capsys.readouterr().err
    assert main([*r, "release", "verify", "R1", "--require-signed"]) == 1
    assert main([*r, "key", "trust", "alice"]) == 0
    assert main([*r, "release", "verify", "R1", "--require-signed"]) == 0
    assert "signed by alice" in capsys.readouterr().out
    assert main([*r, "key", "list"]) == 0
