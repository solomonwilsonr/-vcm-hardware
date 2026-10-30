"""Pure-Python Ed25519 (RFC 8032, section 6), so the core keeps its zero-dependency rule.

Slow by cryptographic-library standards (tens of milliseconds per operation) and not hardened
against timing side channels. That is acceptable for signing release manifests offline; do not
reuse it for anything latency-sensitive or exposed to an attacker who can time it.
"""
from __future__ import annotations

import hashlib

P = 2**255 - 19
Q = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _sha512_modq(data: bytes) -> int:
    return int.from_bytes(_sha512(data), "little") % Q


def _add(a: tuple, b: tuple) -> tuple:
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % P, (a[1] + a[0]) * (b[1] + b[0]) % P
    C, D_ = 2 * a[3] * b[3] * D % P, 2 * a[2] * b[2] % P
    E, F, G, H = B - A, D_ - C, D_ + C, B + A
    return E * F % P, G * H % P, F * G % P, E * H % P


def _mul(s: int, pt: tuple) -> tuple:
    out = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        s >>= 1
    return out


def _equal(a: tuple, b: tuple) -> bool:
    return ((a[0] * b[2] - b[0] * a[2]) % P == 0) and ((a[1] * b[2] - b[1] * a[2]) % P == 0)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P != 0:
        return None
    return P - x if (x & 1) != sign else x


_GY = 4 * pow(5, P - 2, P) % P
_GX = _recover_x(_GY, 0)
G = (_GX, _GY, 1, _GX * _GY % P)


def _compress(pt: tuple) -> bytes:
    zinv = pow(pt[2], P - 2, P)
    x, y = pt[0] * zinv % P, pt[1] * zinv % P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s: bytes) -> tuple | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % P)


def _expand(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("ed25519 seed must be 32 bytes")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, G))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _expand(seed)
    pub = _compress(_mul(a, G))
    r = _sha512_modq(prefix + message)
    r_enc = _compress(_mul(r, G))
    h = _sha512_modq(r_enc + pub + message)
    return r_enc + int.to_bytes((r + h * a) % Q, 32, "little")


def verify(pub: bytes, message: bytes, signature: bytes) -> bool:
    if len(pub) != 32 or len(signature) != 64:
        return False
    a_pt = _decompress(pub)
    r_enc = signature[:32]
    r_pt = _decompress(r_enc)
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= Q:
        return False
    h = _sha512_modq(r_enc + pub + message)
    return _equal(_mul(s, G), _add(r_pt, _mul(h, a_pt)))
