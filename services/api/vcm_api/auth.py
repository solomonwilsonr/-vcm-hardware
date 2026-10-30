"""Bearer-token authentication for the REST API.

Tokens come from the environment, never from the repository:

* ``VCM_API_TOKEN``   one shared token; its holder is identified as ``api``.
* ``VCM_API_TOKENS``  ``alice=tokenA,bob=tokenB``: one token per person, so PR authors and
  approvals are bound to the authenticated identity instead of a free-text field.

The API fails closed: ``create_app`` refuses to build without a token unless ``insecure`` is
passed explicitly.
"""
from __future__ import annotations

import hmac
import os
from collections.abc import Mapping

DEFAULT_IDENTITY = "api"


class AuthConfigError(RuntimeError):
    """No usable token configuration and ``insecure`` was not requested."""


def load_tokens(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return ``{identity: token}`` from the environment (empty when none is set)."""
    env = os.environ if env is None else env
    out: dict[str, str] = {}
    for item in filter(None, (p.strip() for p in env.get("VCM_API_TOKENS", "").split(","))):
        ident, sep, token = item.partition("=")
        if not sep or not ident.strip() or not token.strip():
            raise AuthConfigError("VCM_API_TOKENS entries must look like name=token")
        out[ident.strip()] = token.strip()
    single = env.get("VCM_API_TOKEN", "").strip()
    if single:
        out.setdefault(DEFAULT_IDENTITY, single)
    return out


def identify(tokens: Mapping[str, str], presented: str | None) -> str | None:
    """Identity owning ``presented``, or None. Compares every token in constant time."""
    if not presented:
        return None
    match = None
    for ident, token in tokens.items():
        if hmac.compare_digest(token.encode(), presented.encode()):
            match = ident
    return match


def bearer(header: str | None) -> str | None:
    scheme, _, value = (header or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None
