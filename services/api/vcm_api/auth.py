"""Bearer-token authentication. Identity comes from the token, never from the request body.

Tokens are read from the environment so they never land in a repository:

* ``VCM_API_TOKEN``  one token; the identity is ``$VCM_API_USER`` (default ``api``), role
  ``$VCM_API_ROLE`` (default ``maintainer``: a single-token setup is a single trusted user)
* ``VCM_API_TOKENS`` ``alice=tokA:maintainer,bob=tokB`` for several identities; the role defaults
  to ``writer``

Roles are ordered ``reader`` < ``writer`` < ``maintainer``. Readers may GET; writers may also
create and comment (PRs, releases, devices, locks, validation); maintainers may also merge PRs,
sign releases and break other people's locks.

The API refuses to start with neither set unless ``insecure=True`` is passed explicitly.
"""
from __future__ import annotations

import hmac
import os

from vcm_core import VcmError

PUBLIC = {"/api/health"}
MIN_TOKEN_LEN = 16
ROLES = ("reader", "writer", "maintainer")
DEFAULT_ROLE = "writer"


def required_role(method: str, path: str, query: dict | None = None) -> str:
    if method in ("GET", "HEAD", "OPTIONS"):
        return "reader"
    if path.endswith(("/merge", "/signatures")):
        return "maintainer"
    if method == "DELETE" and path == "/api/locks" and (query or {}).get("force", "").lower() in (
            "1", "true", "yes"):
        return "maintainer"
    return "writer"


def allows(role: str, needed: str) -> bool:
    return ROLES.index(role) >= ROLES.index(needed)


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise VcmError(f"unknown role {role!r} (use one of {', '.join(ROLES)})")
    return role


def normalize_tokens(tokens: dict) -> dict[str, tuple[str, str]]:
    """Accept ``token -> identity`` (role: writer) or ``token -> (identity, role)``."""
    out = {}
    for tok, v in tokens.items():
        who, role = (v, DEFAULT_ROLE) if isinstance(v, str) else v
        if len(tok) < MIN_TOKEN_LEN:
            raise VcmError(f"token for {who!r} is shorter than {MIN_TOKEN_LEN} characters")
        out[tok] = (who, _check_role(role))
    return out


def tokens_from_env(env=None) -> dict[str, tuple[str, str]]:
    """token -> (identity, role)."""
    env = os.environ if env is None else env
    out: dict = {}
    if env.get("VCM_API_TOKEN"):
        out[env["VCM_API_TOKEN"]] = (env.get("VCM_API_USER") or "api",
                                     env.get("VCM_API_ROLE") or "maintainer")
    for item in filter(None, (env.get("VCM_API_TOKENS") or "").split(",")):
        who, sep, rest = item.strip().partition("=")
        tok, _, role = rest.rpartition(":") if rest.rpartition(":")[2] in ROLES else (rest, "", DEFAULT_ROLE)
        if not sep or not who or not tok:
            raise VcmError("VCM_API_TOKENS must look like alice=token:maintainer,bob=token")
        out[tok] = (who, role)
    return normalize_tokens(out)


def identity_for(header: str | None, tokens: dict) -> tuple[str, str] | None:
    """``(identity, role)`` for an ``Authorization`` header value, compared in constant time."""
    scheme, _, presented = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return None
    found = None
    for tok, who in tokens.items():  # no early exit: timing must not reveal which token matched
        if hmac.compare_digest(tok.encode(), presented.strip().encode()):
            found = who
    return found
