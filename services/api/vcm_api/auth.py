"""Bearer-token authentication. Identity comes from the token, never from the request body.

Tokens are read from the environment so they never land in a repository:

* ``VCM_API_TOKEN``  one token; the identity is ``$VCM_API_USER`` (default ``api``)
* ``VCM_API_TOKENS`` ``alice=tokA,bob=tokB`` for several named identities

The API refuses to start with neither set unless ``insecure=True`` is passed explicitly.
"""
from __future__ import annotations

import hmac
import os

from vcm_core import VcmError

PUBLIC = {"/api/health"}
MIN_TOKEN_LEN = 16


def tokens_from_env(env=None) -> dict[str, str]:
    """token -> identity."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    if env.get("VCM_API_TOKEN"):
        out[env["VCM_API_TOKEN"]] = env.get("VCM_API_USER") or "api"
    for item in filter(None, (env.get("VCM_API_TOKENS") or "").split(",")):
        who, sep, tok = item.strip().partition("=")
        if not sep or not who or not tok:
            raise VcmError("VCM_API_TOKENS must look like alice=token,bob=token")
        out[tok] = who
    short = [who for tok, who in out.items() if len(tok) < MIN_TOKEN_LEN]
    if short:
        raise VcmError(f"token for {short[0]!r} is shorter than {MIN_TOKEN_LEN} characters")
    return out


def identity_for(header: str | None, tokens: dict[str, str]) -> str | None:
    """Identity for an ``Authorization`` header value, compared in constant time."""
    scheme, _, presented = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return None
    found = None
    for tok, who in tokens.items():  # no early exit: timing must not reveal which token matched
        if hmac.compare_digest(tok.encode(), presented.strip().encode()):
            found = who
    return found
