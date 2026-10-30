"""API tokens bound to a user identity and a role.

Only the sha256 of a token is stored (``.vcm/tokens.json``), so reading the file does not let anyone
use the API. The plaintext token is shown once, when it is created.

Roles are ordered; each includes everything below it:

* ``viewer``   read anything
* ``editor``   create releases, register units, open reviews, comment, run validation
* ``approver`` approve reviews
* ``admin``    everything
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
from pathlib import Path

from .objects import sha256_bytes

ROLES = ("viewer", "editor", "approver", "admin")
USER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


class TokenError(ValueError):
    pass


def role_at_least(role: str, needed: str) -> bool:
    return role in ROLES and ROLES.index(role) >= ROLES.index(needed)


def _hash(token: str) -> str:
    return sha256_bytes(token.encode())


class TokenStore:
    def __init__(self, vcm_dir: str | Path):
        self.path = Path(vcm_dir) / "tokens.json"

    def _load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.is_file() else {"tokens": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def create(self, user: str, role: str, created_at: str) -> str:
        """Mint a token for ``user``. Returns the plaintext, which is not stored anywhere."""
        if not USER_RE.match(user):
            raise TokenError(f"invalid user name: {user!r}")
        if role not in ROLES:
            raise TokenError(f"unknown role {role!r}; choose from {', '.join(ROLES)}")
        token = "vcm_" + secrets.token_urlsafe(32)
        data = self._load()
        data["tokens"][_hash(token)] = {"user": user, "role": role, "created_at": created_at,
                                        "id": _hash(token)[:12]}
        self._save(data)
        return token

    def list(self) -> list[dict]:
        return sorted(self._load()["tokens"].values(), key=lambda t: (t["user"], t["id"]))

    def revoke(self, token_id: str) -> int:
        data = self._load()
        gone = [h for h, t in data["tokens"].items() if t["id"] == token_id]
        for h in gone:
            del data["tokens"][h]
        self._save(data)
        return len(gone)

    def configured(self) -> bool:
        return bool(self._load()["tokens"])

    def identify(self, token: str) -> dict | None:
        """The identity a presented token maps to, compared in constant time."""
        presented = _hash(token)
        found = None
        for h, entry in self._load()["tokens"].items():
            if hmac.compare_digest(h, presented):
                found = entry
        return found
