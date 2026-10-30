"""Signing keys, the trust database, and detached Ed25519 signatures over digests.

Private keys live outside the repository (``~/.vcm/keys`` or ``$VCM_KEY_HOME``). Public keys live in
``trustdb.json`` at the repository root, so the set of trusted signers is itself committed and every
change to it shows up in history. Verification needs nothing but that file: it is fully offline.

Every signature covers ``vcm-sig/1:<purpose>:<digest>`` rather than the bare digest, so a signature
made for a release can never be replayed as a signature on a device passport, or vice versa.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path

from . import ed25519
from .objects import sha256_bytes

TRUSTDB = "trustdb.json"
ALGORITHM = "ed25519"
KEY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SigningError(ValueError):
    pass


def key_home() -> Path:
    return Path(os.environ.get("VCM_KEY_HOME", Path.home() / ".vcm" / "keys"))


def key_id(public_hex: str) -> str:
    return sha256_bytes(bytes.fromhex(public_hex))[:16]


def _message(purpose: str, digest: str) -> bytes:
    return f"vcm-sig/1:{purpose}:{digest}".encode()


def generate_key(name: str, home: Path | None = None) -> dict:
    """Create a private key file readable only by its owner. Refuses to overwrite one."""
    if not KEY_NAME_RE.match(name):
        raise SigningError(f"invalid key name: {name!r}")
    home = home or key_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / f"{name}.key"
    secret = secrets.token_bytes(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SigningError(f"key already exists: {path}") from exc
    with os.fdopen(fd, "w") as f:
        f.write(secret.hex() + "\n")
    pub = ed25519.public_key(secret).hex()
    return {"name": name, "public_key": pub, "key_id": key_id(pub), "path": str(path)}


def load_secret(name: str, home: Path | None = None) -> bytes:
    path = (home or key_home()) / f"{name}.key"
    if not path.is_file():
        raise SigningError(f"no private key named {name!r} in {path.parent}")
    return bytes.fromhex(path.read_text().strip())


def public_from_secret(secret: bytes) -> str:
    return ed25519.public_key(secret).hex()


# ---- trust database ------------------------------------------------------------
def trust_path(root: str | Path) -> Path:
    override = os.environ.get("VCM_TRUSTDB")
    return Path(override) if override else Path(root) / TRUSTDB


def load_trust(root: str | Path) -> dict:
    p = trust_path(root)
    if not p.is_file():
        return {"schema": "vcm-trust/1", "keys": {}}
    data = json.loads(p.read_text())
    data.setdefault("keys", {})
    return data


def trust_key(root: str | Path, name: str, public_hex: str, added_at: str,
              roles: list[str] | None = None) -> dict:
    """Add a public key to the trust database (a working-tree change: commit it)."""
    try:
        if len(bytes.fromhex(public_hex)) != 32:
            raise ValueError
    except ValueError as exc:
        raise SigningError("public key must be 32 bytes of hex") from exc
    data = load_trust(root)
    kid = key_id(public_hex)
    data["keys"][kid] = {"name": name, "public_key": public_hex, "added_at": added_at,
                         "roles": sorted(roles or ["release", "passport"])}
    p = trust_path(root)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    return {"key_id": kid, **data["keys"][kid]}


def revoke_key(root: str | Path, kid: str) -> None:
    data = load_trust(root)
    if kid not in data["keys"]:
        raise SigningError(f"no trusted key {kid}")
    del data["keys"][kid]
    trust_path(root).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


# ---- signatures ----------------------------------------------------------------
def sign_digest(secret: bytes, signer: str, purpose: str, digest: str, signed_at: str) -> dict:
    pub = public_from_secret(secret)
    sig = ed25519.sign(secret, _message(purpose, digest))
    return {"algorithm": ALGORITHM, "key_id": key_id(pub), "signer": signer,
            "purpose": purpose, "signed_at": signed_at, "signature": sig.hex()}


def verify_signature(trust: dict, sig: dict, purpose: str, digest: str) -> tuple[bool, str]:
    """``(ok, reason)``. A valid signature by a key that is not trusted is *not* ok."""
    if sig.get("algorithm") != ALGORITHM:
        return False, f"unsupported algorithm {sig.get('algorithm')!r}"
    if sig.get("purpose") != purpose:
        return False, f"signature is for {sig.get('purpose')!r}, not {purpose!r}"
    entry = (trust.get("keys") or {}).get(sig.get("key_id", ""))
    if entry is None:
        return False, f"key {sig.get('key_id')} is not in the trust database"
    if purpose not in entry.get("roles", [purpose]):
        return False, f"key {sig['key_id']} ({entry['name']}) is not trusted to sign {purpose}"
    try:
        ok = ed25519.verify(bytes.fromhex(entry["public_key"]), _message(purpose, digest),
                            bytes.fromhex(sig.get("signature", "")))
    except ValueError:
        ok = False
    return (True, f"signed by {entry['name']} ({sig['key_id']})") if ok else (
        False, f"signature by {sig['key_id']} does not match the content")
