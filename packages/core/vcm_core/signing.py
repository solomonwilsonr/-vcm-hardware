"""Release signing: private keys, the committed trust DB, and manifest signatures.

Private keys live outside the repository (``$VCM_KEY_DIR`` or ``~/.vcm/keys``) as a hex seed
with mode 0600. Public keys live in ``trustdb.json`` at the repository root, so the set of
people allowed to sign is itself version-controlled and verification works offline.

A signature covers ``SIGN_DOMAIN + manifest_sha256``. The domain prefix keeps a release
signature from being replayed as anything else; signing the digest covers every manifest field.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from . import ed25519
from .repo import Repository, VcmError, now_iso

ALGORITHM = "ed25519"
SIGN_DOMAIN = b"vcm-release-signature/1\n"
TRUSTDB = "trustdb.json"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def key_dir() -> Path:
    return Path(os.environ.get("VCM_KEY_DIR") or Path.home() / ".vcm" / "keys")


def _check_name(name: str) -> str:
    if not _NAME.match(name):
        raise VcmError(f"invalid key name: {name!r}")
    return name


def generate_key(name: str) -> str:
    """Create a new private key and return its public key (hex). Never overwrites."""
    path = key_dir() / f"{_check_name(name)}.key"
    if path.exists():
        raise VcmError(f"key already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(seed.hex() + "\n")
    return ed25519.public_key(seed).hex()


def load_seed(name: str) -> bytes:
    path = key_dir() / f"{_check_name(name)}.key"
    try:
        return bytes.fromhex(path.read_text().strip())
    except FileNotFoundError:
        raise VcmError(f"no private key {name!r} in {path.parent} (run: vcm key gen {name})") from None
    except ValueError:
        raise VcmError(f"private key file is not valid hex: {path}") from None


def own_public_key(name: str) -> str:
    return ed25519.public_key(load_seed(name)).hex()


def local_keys() -> list[str]:
    d = key_dir()
    return sorted(p.stem for p in d.glob("*.key")) if d.is_dir() else []


# ---- trust DB ------------------------------------------------------------------------


def _trust_path(repo: Repository) -> Path:
    return repo.root / TRUSTDB


def load_trust(repo: Repository) -> dict[str, dict]:
    p = _trust_path(repo)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())["keys"]
    except (ValueError, KeyError, TypeError):
        raise VcmError(f"{TRUSTDB} is malformed") from None


def trust_key(repo: Repository, name: str, public_hex: str) -> None:
    """Add (or, for the same key, refresh) a trusted public key. Refuses to swap a key silently."""
    _check_name(name)
    try:
        if len(bytes.fromhex(public_hex)) != 32:
            raise ValueError
    except ValueError:
        raise VcmError("public key must be 64 hex characters (32 bytes)") from None
    keys = load_trust(repo)
    if name in keys and keys[name]["public_key"] != public_hex:
        raise VcmError(f"{name!r} is already trusted with a different key; remove it from "
                       f"{TRUSTDB} deliberately if the key was rotated")
    keys.setdefault(name, {"public_key": public_hex, "algorithm": ALGORITHM, "added_at": now_iso()})
    _trust_path(repo).write_text(json.dumps({"keys": keys}, indent=2, sort_keys=True) + "\n")


# ---- signatures ----------------------------------------------------------------------


def _message(manifest_sha256: str) -> bytes:
    return SIGN_DOMAIN + manifest_sha256.encode()


def make_signature(name: str, manifest_sha256: str) -> dict:
    seed = load_seed(name)
    return {"key_id": name, "algorithm": ALGORITHM, "public_key": ed25519.public_key(seed).hex(),
            "signature": ed25519.sign(seed, _message(manifest_sha256)).hex(), "signed_at": now_iso()}


def check_signature(sig: dict, manifest_sha256: str, trust: dict[str, dict]) -> tuple[bool, str]:
    """(ok, detail). Valid only if the maths verify AND the key is trusted under that name."""
    who = sig.get("key_id", "?")
    if sig.get("algorithm") != ALGORITHM:
        return False, f"{who}: unsupported algorithm {sig.get('algorithm')!r}"
    try:
        valid = ed25519.verify(bytes.fromhex(sig["public_key"]), _message(manifest_sha256),
                               bytes.fromhex(sig["signature"]))
    except (KeyError, ValueError):
        return False, f"{who}: malformed signature"
    if not valid:
        return False, f"{who}: signature does not match the manifest"
    entry = trust.get(who)
    if entry is None:
        return False, f"{who}: valid signature but key is not in {TRUSTDB}"
    if entry["public_key"] != sig["public_key"]:
        return False, f"{who}: signed with a key other than the one trusted for that name"
    return True, f"{who}: valid, trusted"
