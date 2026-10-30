"""Detached Ed25519 signatures over digests, a local key store and a committed trust set.

A signature proves *authenticity*, not just consistency: it can only be produced by whoever holds
the private key. Private keys live in ``~/.vcm/keys`` (override with $VCM_KEY_DIR) and never enter
a repository. The public keys a project trusts live in ``trustdb.json`` next to the design, so the
trust set is itself version-controlled and verification needs no network.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import ed25519
from .model import canonical_json
from .objects import sha256_bytes
from .repo import VcmError, now_iso

ALG = "ed25519"
TRUSTDB = "trustdb.json"
KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def key_dir() -> Path:
    return Path(os.environ.get("VCM_KEY_DIR") or Path.home() / ".vcm" / "keys")


def key_id(public_hex: str) -> str:
    """Short stable identifier: first 16 hex chars of sha256 of the raw public key."""
    return sha256_bytes(bytes.fromhex(public_hex))[:16]


def _key_path(name: str) -> Path:
    if not KEY_NAME_RE.match(name):
        raise VcmError(f"invalid key name: {name!r}")
    return key_dir() / f"{name}.key"


def generate_key(name: str) -> dict:
    """Create ``name`` in the local key store (mode 0600); returns the public half."""
    path = _key_path(name)
    if path.exists():
        raise VcmError(f"key already exists: {name}")
    seed = os.urandom(32)
    pub = ed25519.public_key(seed).hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"name": name, "alg": ALG, "public": pub, "secret": seed.hex(), "created_at": now_iso()}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(body, indent=2) + "\n")
    return public_info(name)


def _load_key(name: str) -> dict:
    path = _key_path(name)
    if not path.is_file():
        raise VcmError(f"no such key: {name} (create it with `vcm key gen {name}`)")
    return json.loads(path.read_text())


def public_info(name: str) -> dict:
    k = _load_key(name)
    return {"name": k["name"], "alg": k["alg"], "public": k["public"], "key_id": key_id(k["public"])}


def list_keys() -> list[dict]:
    d = key_dir()
    return [public_info(p.stem) for p in sorted(d.glob("*.key"))] if d.is_dir() else []


# ---- trust set ------------------------------------------------------------------------
def trustdb_path(root: str | Path) -> Path:
    return Path(root) / TRUSTDB


def load_trustdb(root: str | Path) -> dict:
    p = trustdb_path(root)
    if not p.is_file():
        return {"schema": "vcm-trust/1", "keys": {}}
    return json.loads(p.read_text())


def _save_trustdb(root: str | Path, db: dict) -> None:
    trustdb_path(root).write_text(json.dumps(db, indent=2, sort_keys=True) + "\n")


def trust_key(root: str | Path, name: str, public_hex: str) -> dict:
    try:
        raw = bytes.fromhex(public_hex)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise VcmError("a public key is 64 hex characters (32 bytes)")
    db = load_trustdb(root)
    kid = key_id(public_hex)
    db["keys"][kid] = {"name": name, "alg": ALG, "public": public_hex, "added_at": now_iso()}
    _save_trustdb(root, db)
    return {"key_id": kid, **db["keys"][kid]}


def untrust_key(root: str | Path, kid: str) -> None:
    db = load_trustdb(root)
    if kid not in db["keys"]:
        raise VcmError(f"key {kid} is not trusted")
    del db["keys"][kid]
    _save_trustdb(root, db)


# ---- signatures -----------------------------------------------------------------------
def _message(purpose: str, digest: str) -> bytes:
    """Domain-separated: a release signature can never be replayed as a passport signature."""
    return f"vcm-sig/1:{purpose}:{digest}".encode()


def document_digest(doc: dict) -> str:
    """sha256 of the canonical JSON of ``doc`` with its ``signatures`` field excluded."""
    return sha256_bytes(canonical_json({k: v for k, v in doc.items() if k != "signatures"}))


def sign_digest(key_name: str, digest: str, purpose: str) -> dict:
    k = _load_key(key_name)
    sig = ed25519.sign(bytes.fromhex(k["secret"]), _message(purpose, digest))
    return {"alg": ALG, "key_id": key_id(k["public"]), "name": k["name"], "public": k["public"],
            "signature": sig.hex(), "signed_at": now_iso()}


def verify_signature(sig: dict, digest: str, purpose: str, root: str | Path) -> dict:
    """Check one signature record. ``valid`` is cryptographic; ``trusted`` is membership of the
    project's trustdb. Only a signature that is both counts as authenticated."""
    out = {"key_id": sig.get("key_id", "?"), "name": sig.get("name", "?"), "valid": False,
           "trusted": False, "detail": ""}
    try:
        pub, raw = bytes.fromhex(sig["public"]), bytes.fromhex(sig["signature"])
    except (KeyError, ValueError):
        out["detail"] = "malformed signature record"
        return out
    if sig.get("alg") != ALG:
        out["detail"] = f"unsupported algorithm {sig.get('alg')!r}"
        return out
    if key_id(sig["public"]) != sig.get("key_id"):
        out["detail"] = "key_id does not match the embedded public key"
        return out
    out["valid"] = ed25519.verify(pub, _message(purpose, digest), raw)
    if not out["valid"]:
        out["detail"] = "signature does not match the signed digest"
        return out
    entry = load_trustdb(root)["keys"].get(out["key_id"])
    out["trusted"] = bool(entry) and entry["public"] == sig["public"]
    out["detail"] = "" if out["trusted"] else "valid, but the key is not in trustdb.json"
    return out


def add_signature(doc: dict, sig: dict) -> dict:
    """Append ``sig`` to ``doc['signatures']``, replacing an earlier one from the same key."""
    kept = [s for s in doc.get("signatures", []) if s.get("key_id") != sig["key_id"]]
    doc["signatures"] = [*kept, sig]
    return doc
