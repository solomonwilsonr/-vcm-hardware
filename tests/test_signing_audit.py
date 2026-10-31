import json

import pytest

from vcm_cli.main import main
from vcm_core import AuditLog, VcmError, ed25519
from vcm_core import signing as sg
from vcm_trace import passport, register_device, sign_device
from vcm_workflow import create_release, sign_release, verify_release


@pytest.fixture(autouse=True)
def key_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VCM_KEY_HOME", str(tmp_path / "keys"))
    monkeypatch.delenv("VCM_TRUSTDB", raising=False)
    return tmp_path / "keys"


def _trusted(repo, name="alice", roles=None):
    k = sg.generate_key(name)
    sg.trust_key(repo.root, name, k["public_key"], "2026-01-01T00:00:00Z", roles)
    return k


# ---- Ed25519 -----------------------------------------------------------------------
def test_ed25519_rfc8032_vectors():
    sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert ed25519.public_key(sk).hex() == (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    assert ed25519.sign(sk, b"").hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e3970"
        "1cf9b46bd25bf5f0595bbe24655141438e7a100b")
    sk3 = bytes.fromhex("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7")
    msg = bytes.fromhex("af82")
    sig = ed25519.sign(sk3, msg)
    assert sig.hex() == (
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760"
        "984dc6594a7c15e9716ed28dc027beceea1ec40a")
    assert ed25519.verify(ed25519.public_key(sk3), msg, sig)


def test_ed25519_rejects_tampering():
    sk = bytes(range(32))
    pub = ed25519.public_key(sk)
    sig = ed25519.sign(sk, b"release R1")
    assert ed25519.verify(pub, b"release R1", sig)
    assert not ed25519.verify(pub, b"release R2", sig)
    bad = bytearray(sig)
    bad[5] ^= 1
    assert not ed25519.verify(pub, b"release R1", bytes(bad))
    assert not ed25519.verify(pub, b"release R1", sig[:63])


# ---- audit log ---------------------------------------------------------------------
def test_audit_chain_records_and_detects_edits(sample_repo):
    log = sample_repo.audit
    actions = [e["action"] for e in log.events()]
    assert actions == ["commit", "tag", "commit", "tag"]
    assert log.events()[0]["actor"] == "alice"
    assert log.verify()["ok"]

    lines = log.path.read_text().splitlines()
    ev = json.loads(lines[1])
    ev["subject"] = "v9.9"
    lines[1] = json.dumps(ev, sort_keys=True)
    log.path.write_text("\n".join(lines) + "\n")
    res = AuditLog(sample_repo.dir).verify()
    assert not res["ok"] and res["broken_at"] == 2 and "hash" in res["reason"]


def test_audit_detects_deleted_event(sample_repo):
    lines = sample_repo.audit.path.read_text().splitlines()
    sample_repo.audit.path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    assert not sample_repo.audit.verify()["ok"]


# ---- release signatures ------------------------------------------------------------
def test_release_sign_and_verify(sample_repo):
    _trusted(sample_repo)
    m = create_release(sample_repo, "R1", "v1.1")
    assert m["audit_head"] == sample_repo.audit.events()[-2]["hash"]  # before release.create
    res = verify_release(sample_repo, "R1")
    assert res["ok"] and not res["signed"]
    assert not verify_release(sample_repo, "R1", require_signature=True)["ok"]

    sign_release(sample_repo, "R1", "alice")
    res = verify_release(sample_repo, "R1", require_signature=True)
    assert res["ok"] and res["signed"] and res["valid_signatures"] == 1
    assert sample_repo.audit.events()[-1]["action"] == "release.sign"


def test_forged_release_fails_signature(sample_repo):
    _trusted(sample_repo)
    create_release(sample_repo, "R1", "v1.1")
    sign_release(sample_repo, "R1", "alice")
    p = sample_repo.dir / "releases" / "R1.json"
    m = json.loads(p.read_text())
    # an attacker who rewrites the manifest and recomputes its hash still cannot re-sign it
    from vcm_workflow.release import _manifest_hash
    m["revision_message"] = "totally fine"
    m["manifest_sha256"] = _manifest_hash(m)
    p.write_text(json.dumps(m))
    res = verify_release(sample_repo, "R1", require_signature=True)
    assert not res["ok"]
    assert any(c["check"].startswith("signature:") and not c["ok"] for c in res["checks"])


def test_untrusted_or_revoked_key_does_not_verify(sample_repo):
    k = _trusted(sample_repo)
    create_release(sample_repo, "R1", "v1.1")
    sign_release(sample_repo, "R1", "alice")
    sg.revoke_key(sample_repo.root, k["key_id"])
    res = verify_release(sample_repo, "R1", require_signature=True)
    assert not res["ok"] and not res["signed"]
    sg.generate_key("mallory")
    with pytest.raises(VcmError, match="not in the trust database"):
        sign_release(sample_repo, "R1", "mallory")


def test_signature_purpose_is_bound(sample_repo):
    _trusted(sample_repo, roles=["passport"])
    create_release(sample_repo, "R1", "v1.1")
    with pytest.raises(VcmError, match="not trusted to sign release"):
        sign_release(sample_repo, "R1", "alice")
    sig = sg.sign_digest(sg.load_secret("alice"), "alice", "passport", "ab" * 32, "now")
    ok, why = sg.verify_signature(sg.load_trust(sample_repo.root), sig, "release", "ab" * 32)
    assert not ok and "not 'release'" in why


def test_key_files_are_private_and_not_overwritten(key_home):
    k = sg.generate_key("ops")
    assert (key_home / "ops.key").stat().st_mode & 0o777 == 0o600
    with pytest.raises(sg.SigningError):
        sg.generate_key("ops")
    with pytest.raises(sg.SigningError):
        sg.generate_key("../escape")
    assert len(k["key_id"]) == 16


# ---- passports ---------------------------------------------------------------------
def test_device_passport_signature(sample_repo):
    _trusted(sample_repo)
    create_release(sample_repo, "R1", "v1.1")
    register_device(sample_repo, "SN1", "R1")
    assert not passport(sample_repo, "SN1")["authenticity"]["signed"]
    sign_device(sample_repo, "SN1", "alice")
    auth = passport(sample_repo, "SN1")["authenticity"]
    assert auth["signed"] and auth["signatures"][0]["ok"]

    p = sample_repo.dir / "devices" / "SN1.json"
    rec = json.loads(p.read_text())
    rec["release"] = "R0"
    p.write_text(json.dumps(rec))
    from vcm_trace import verify_device
    assert not verify_device(sample_repo, rec)["signed"]


# ---- CLI ---------------------------------------------------------------------------
def test_cli_key_sign_verify_audit(sample_repo, capsys):
    root = str(sample_repo.root)
    assert main(["-C", root, "key", "gen", "carol", "--trust"]) == 0
    assert main(["-C", root, "release", "create", "R1", "--rev", "v1.1"]) == 0
    assert main(["-C", root, "release", "verify", "R1", "--require-signature"]) == 1
    assert main(["-C", root, "release", "sign", "R1", "--key", "carol"]) == 0
    assert main(["-C", root, "release", "verify", "R1", "--require-signature"]) == 0
    assert main(["-C", root, "key", "list"]) == 0
    assert main(["-C", root, "audit", "verify"]) == 0
    capsys.readouterr()
    assert main(["-C", root, "audit", "log", "--json"]) == 0
    actions = [e["action"] for e in json.loads(capsys.readouterr().out)]
    assert {"key.trust", "release.create", "release.sign"} <= set(actions)
