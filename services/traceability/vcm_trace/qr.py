"""Tiny pure-Python QR encoder: byte mode, error correction level M, versions 1-10 (up to 213 bytes).

Follows ISO/IEC 18004 for the supported subset. Output is a boolean module matrix or an SVG string.
"""
from __future__ import annotations

# version -> (EC codewords per block, [(block count, data codewords per block), ...]) for level M
_EC_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _gmul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    gen = [1]
    for i in range(degree):
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            nxt[j + 1] ^= _gmul(c, _EXP[i])
        gen = nxt
    return gen


def rs_remainder(data: list[int], degree: int) -> list[int]:
    gen = _rs_generator(degree)
    rem = list(data) + [0] * degree
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gmul(gen[j], coef)
    return rem[len(data):]


def _capacity(version: int) -> int:
    _, blocks = _EC_M[version]
    return sum(n * d for n, d in blocks)


def _bits(value: int, n: int) -> list[int]:
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def _codewords(data: bytes, version: int) -> list[int]:
    cap_bits = _capacity(version) * 8
    bits = _bits(0b0100, 4) + _bits(len(data), 8 if version < 10 else 16)
    for byte in data:
        bits += _bits(byte, 8)
    bits += [0] * min(4, cap_bits - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(words) < _capacity(version):
        words.append(pad[(len(words) - len(bits) // 8) % 2])
    ec_len, groups = _EC_M[version]
    blocks, pos = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[pos:pos + size])
            pos += size
    ecs = [rs_remainder(b, ec_len) for b in blocks]
    out = [b[i] for i in range(max(map(len, blocks))) for b in blocks if i < len(b)]
    out += [e[i] for i in range(ec_len) for e in ecs]
    return out


def _format_bits(mask: int) -> int:
    data = mask  # error correction level M is 0b00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


class _Matrix:
    def __init__(self, version: int):
        self.version, self.size = version, 17 + 4 * version
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns()

    def _set(self, x: int, y: int, dark: bool) -> None:
        self.m[y][x] = dark
        self.fn[y][x] = True

    def _finder(self, cx: int, cy: int) -> None:
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    def _draw_function_patterns(self) -> None:
        n = self.size
        for i in range(n):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            self._finder(cx, cy)
        pos = _ALIGN[self.version]
        for i, cy in enumerate(pos):
            for j, cx in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)  # reserve areas
        if self.version >= 7:
            rem = self.version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = (self.version << 12) | rem
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self._set(a, b, bool((bits >> i) & 1))
                self._set(b, a, bool((bits >> i) & 1))

    def draw_format(self, mask: int) -> None:
        bits, n = _format_bits(mask), self.size

        def bit(i: int) -> bool:
            return bool((bits >> i) & 1)

        for i in range(6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(8):
            self._set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, n - 15 + i, bit(i))
        self._set(8, n - 8, True)

    def place_data(self, words: list[int]) -> None:
        n, i = self.size, 0
        for right in range(n - 1, 0, -2):
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and i < len(words) * 8:
                        self.m[y][x] = bool((words[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1

    def apply_mask(self, mask: int) -> None:
        f = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0][mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self) -> int:
        n, m, score = self.size, self.m, 0
        lines = [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
        for line in lines:
            run = 1
            for i in range(1, n + 1):
                if i < n and line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run = 1
            s = "".join("1" if c else "0" for c in line)
            for pat in ("10111010000", "00001011101"):
                start = 0
                while (k := s.find(pat, start)) != -1:
                    score += 40
                    start = k + 1
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, m))
        total = n * n
        score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return score


def make_qr(text: str | bytes) -> list[list[bool]]:
    data = text.encode() if isinstance(text, str) else text
    version = next((v for v in _EC_M if 4 + (8 if v < 10 else 16) + 8 * len(data) <= _capacity(v) * 8), None)
    if version is None:
        raise ValueError(f"payload too long for QR versions 1-10 level M ({len(data)} bytes)")
    words = _codewords(data, version)
    best: tuple[int, int, list[list[bool]]] | None = None
    for mask in range(8):
        mx = _Matrix(version)
        mx.place_data(words)
        mx.apply_mask(mask)
        mx.draw_format(mask)
        pen = mx.penalty()
        if best is None or pen < best[0]:
            best = (pen, mask, mx.m)
    assert best is not None
    return best[2]


def qr_svg(text: str | bytes, scale: int = 8, border: int = 4) -> str:
    m = make_qr(text)
    n = len(m)
    size = (n + 2 * border) * scale
    cells = "".join(f"M{(x + border) * scale},{(y + border) * scale}h{scale}v{scale}h-{scale}z"
                    for y in range(n) for x in range(n) if m[y][x])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" '
            f'height="{size}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{cells}" fill="#000"/></svg>\n')
