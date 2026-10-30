import json

import pytest

from vcm_core import VcmError, ed25519, signing
from vcm_trace import register_device, sign_passport, verify_passport
from vcm_workflow import create_release, sign_release, verify_release


@pytest.fixture(autouse=True)
def keydir(tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_KEY_DIR", str(tmp_path / "keys"))


def test_ed25519_rfc8032_vectors():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pub = ed25519.public_key(seed)
    assert pub.hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig = ed25519.sign(seed, b"")
    assert sig.hex() == ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb88215"
                         "90a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert ed25519.verify(pub, b"", sig)
    assert not ed25519.verify(pub, b"x", sig)
    assert not ed25519.verify(pub, b"", sig[:-1] + bytes([sig[-1] ^ 1]))
    seed2 = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    sig2 = ed25519.sign(seed2, bytes.fromhex("72"))
    assert sig2.hex().startswith("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da")


def test_key_store_is_private_and_not_overwritable():
    info = signing.generate_key("alice")
    path = signing.key_dir() / "alice.key"
    assert path.stat().st_mode & 0o777 == 0o600
    assert info["key_id"] == signing.key_id(info["public"])
    assert [k["name"] for k in signing.list_keys()] == ["alice"]
    with pytest.raises(VcmError):
        signing.generate_key("alice")
    with pytest.raises(VcmError):
        signing.generate_key("../evil")
    assert "secret" not in json.dumps(signing.public_info("alice"))


def test_signed_release_verifies_and_reports_authenticated(sample_repo):
    create_release(sample_repo, "R1", "v1.0")
    unsigned = verify_release(sample_repo, "R1")
    assert unsigned["ok"] and not unsigned["authenticated"]
    assert not verify_release(sample_repo, "R1", require_signature=True)["ok"]

    info = signing.generate_key("alice")
    sign_release(sample_repo, "R1", "alice")
    # valid but the key is not trusted yet: not authenticated, and flagged
    res = verify_release(sample_repo, "R1")
    assert not res["ok"] and not res["authenticated"]
    assert "not in trustdb" in res["problems"][0]["detail"]

    signing.trust_key(sample_repo.root, "alice", info["public"])
    res = verify_release(sample_repo, "R1", require_signature=True)
    assert res["ok"] and res["authenticated"]
    assert res["signers"][0]["name"] == "alice"


def test_signing_does_not_change_manifest_hash_and_resign_replaces(sample_repo):
    m = create_release(sample_repo, "R1", "v1.0")
    signing.generate_key("alice")
    signed = sign_release(sample_repo, "R1", "alice")
    assert signed["manifest_sha256"] == m["manifest_sha256"]
    assert len(sign_release(sample_repo, "R1", "alice")["signatures"]) == 1


def test_tampering_after_signing_is_caught(sample_repo):
    create_release(sample_repo, "R1", "v1.0")
    info = signing.generate_key("alice")
    signing.trust_key(sample_repo.root, "alice", info["public"])
    sign_release(sample_repo, "R1", "alice")
    m = sample_repo.load_record("releases", "R1")
    m["name"] = "R1"
    m["revision_message"] = "forged"
    sample_repo.save_record("releases", "R1", m)  # attacker edits the manifest, keeps the signature
    res = verify_release(sample_repo, "R1", require_signature=True)
    assert not res["ok"] and "manifest_hash" in {c["check"] for c in res["problems"]}
    # even if the attacker also recomputes the hash, the signature no longer matches it
    from vcm_workflow.release import _manifest_hash
    m["manifest_sha256"] = _manifest_hash(m)
    sample_repo.save_record("releases", "R1", m)
    res = verify_release(sample_repo, "R1", require_signature=True)
    assert not res["ok"] and not res["authenticated"]


def test_signature_from_a_different_trusted_key_does_not_transfer(sample_repo):
    create_release(sample_repo, "R1", "v1.0")
    a, b = signing.generate_key("alice"), signing.generate_key("bob")
    signing.trust_key(sample_repo.root, "bob", b["public"])
    sign_release(sample_repo, "R1", "alice")  # alice is not trusted
    assert not verify_release(sample_repo, "R1", require_signature=True)["authenticated"]
    # swapping bob's public key into alice's record fails the key_id binding
    m = sample_repo.load_record("releases", "R1")
    m["signatures"][0]["public"] = b["public"]
    sample_repo.save_record("releases", "R1", m)
    assert not verify_release(sample_repo, "R1", require_signature=True)["authenticated"]
    assert a["key_id"] != b["key_id"]


def test_purposes_are_domain_separated():
    signing.generate_key("alice")
    sig = signing.sign_digest("alice", "ab" * 32, "release")
    assert signing.verify_signature(sig, "ab" * 32, "release", "/nonexistent")["valid"]
    assert not signing.verify_signature(sig, "ab" * 32, "passport", "/nonexistent")["valid"]


def test_signed_passport_roundtrip_and_tamper(sample_repo):
    create_release(sample_repo, "R1.0", "v1.0")
    register_device(sample_repo, "SN-1", "R1.0", "B")
    info = signing.generate_key("alice")
    signing.trust_key(sample_repo.root, "alice", info["public"])
    doc = sign_passport(sample_repo, "SN-1", "alice")
    assert verify_passport(doc, sample_repo.root)["authenticated"]
    doc["batch"] = "FORGED"
    assert not verify_passport(doc, sample_repo.root)["authenticated"]


def test_trust_rejects_bad_keys_and_untrust(sample_repo):
    with pytest.raises(VcmError):
        signing.trust_key(sample_repo.root, "x", "zz")
    info = signing.generate_key("alice")
    t = signing.trust_key(sample_repo.root, "alice", info["public"])
    signing.untrust_key(sample_repo.root, t["key_id"])
    assert signing.load_trustdb(sample_repo.root)["keys"] == {}
    with pytest.raises(VcmError):
        signing.untrust_key(sample_repo.root, t["key_id"])


def test_revoked_key_stops_counting(sample_repo):
    create_release(sample_repo, "R1", "v1.0")
    info = signing.generate_key("alice")
    signing.trust_key(sample_repo.root, "alice", info["public"])
    sign_release(sample_repo, "R1", "alice")
    assert verify_release(sample_repo, "R1", require_signature=True)["authenticated"]
    signing.revoke_key(sample_repo.root, info["key_id"], "laptop stolen")
    res = verify_release(sample_repo, "R1", require_signature=True)
    assert not res["ok"] and not res["authenticated"]
    assert "revoked" in res["problems"][0]["detail"]
    with pytest.raises(VcmError):
        signing.revoke_key(sample_repo.root, "0" * 16)


def test_encrypted_key_roundtrip_wrong_passphrase_and_tamper(monkeypatch):
    info = signing.generate_key("sealed", passphrase="correct horse")
    assert info["encrypted"]
    raw = (signing.key_dir() / "sealed.key").read_text()
    assert '"secret"' not in raw and "correct horse" not in raw
    with pytest.raises(VcmError, match="encrypted"):
        signing.sign_digest("sealed", "ab" * 32, "release")
    monkeypatch.setenv("VCM_KEY_PASSPHRASE", "wrong")
    with pytest.raises(VcmError, match="wrong passphrase"):
        signing.sign_digest("sealed", "ab" * 32, "release")
    monkeypatch.setenv("VCM_KEY_PASSPHRASE", "correct horse")
    sig = signing.sign_digest("sealed", "ab" * 32, "release")
    assert signing.verify_signature(sig, "ab" * 32, "release", "/nonexistent")["valid"]
    path = signing.key_dir() / "sealed.key"
    body = json.loads(path.read_text())
    body["encrypted"]["ciphertext"] = "00" * 32
    path.write_text(json.dumps(body))
    with pytest.raises(VcmError, match="wrong passphrase|modified"):
        signing.sign_digest("sealed", "ab" * 32, "release")


def test_pure_and_library_backends_agree(monkeypatch):
    if ed25519._lib is None:
        pytest.skip("cryptography not installed")
    monkeypatch.delenv("VCM_PURE_ED25519", raising=False)
    seed = bytes(range(32))
    assert ed25519.backend() == "cryptography"
    assert ed25519.public_key(seed) == ed25519.pure_public_key(seed)
    msg = b"vcm-sig/1:release:abc"
    assert ed25519.sign(seed, msg) == ed25519.pure_sign(seed, msg)  # Ed25519 is deterministic
    sig = ed25519.sign(seed, msg)
    pub = ed25519.public_key(seed)
    assert ed25519.pure_verify(pub, msg, sig) and ed25519.verify(pub, msg, sig)
    assert not ed25519.verify(pub, msg + b"x", sig)
    monkeypatch.setenv("VCM_PURE_ED25519", "1")
    assert ed25519.backend() == "pure-python" and ed25519.verify(pub, msg, sig)
