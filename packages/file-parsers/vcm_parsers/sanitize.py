"""Scrub a machine capture so it can be shared and committed, without breaking what it is for.

Real captures carry serial numbers, hostnames, addresses, UUIDs and sometimes credentials. This
replaces them with stable pseudonyms of the same shape, so the capture still parses the same way
and still correlates across files (a GPU serial in the Redfish capture and in ``nvidia-smi`` map
to the same stand-in), while model names, part numbers and firmware versions stay exactly as they
were: those are what VCM needs and what a parser bug depends on.

What it does, and does not, promise:

* It **does** replace values under identifying keys (serial, UUID, asset tag, hostname, MAC),
  every other occurrence of those exact values, MAC addresses, UUIDs and e-mail addresses
  anywhere, IP addresses under address-like keys and in URLs, URL credentials, and the value of
  any key that looks like a secret.
* It does **not** touch bare dotted numbers in free text, because ``2.14.0.0`` is as likely to be a
  firmware version as an address. It lists them as *possible leaks* for you to look at.
* It cannot know what a site considers sensitive in free-text or vendor-specific fields. Read the
  output before sharing it. The leak check is a safety net, not a guarantee.

Pseudonyms are keyed with a salt (random per run unless you pass one), so low-entropy values such
as serial numbers cannot be recovered by brute force from the output.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from hashlib import sha256

#: values under these keys identify one machine; they and every other occurrence are replaced
IDENT_KEYS = {"serialnumber", "serial", "uuid", "gpu_uuid", "assettag", "asset_tag", "hostname",
              "host_name", "fqdn", "domainname", "macaddress", "permanentmacaddress", "mac",
              "board_serial", "chassisserialnumber", "systemuuid"}
#: any key containing one of these has its whole value redacted
SECRET_HINTS = ("password", "passwd", "passphrase", "secret", "token", "apikey", "api_key",
                "authorization", "community", "privatekey", "private_key", "credential")
#: values under these keys are what VCM reads for matching; they are never rewritten
PROTECTED_KEYS = {"@odata.type", "model", "partnumber", "sku", "manufacturer", "product_name",
                  "board_part_number", "protocol", "processortype", "memorydevicetype"}
#: fields that hold names of servers or domains, whatever the vendor calls them
HOST_HINTS = ("ntp", "dns", "domain", "fqdn", "host", "server")
ADDRESS_HINTS = ("address", "gateway", "dns", "ipv4", "ipv6", "hostname", "host_name", "fqdn")
MIN_LITERAL = 4

_MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
_UUID = re.compile(r"(?<![0-9A-Fa-f-])[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}"
                   r"(?![0-9A-Fa-f-])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_URL = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<auth>[^/@\s\"']*@)?"
                  r"(?P<host>\[[0-9A-Fa-f:.]+\]|[^/:\s?#\"']+)")
_QUAD = re.compile(r"(?<![0-9A-Za-z.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9A-Za-z.])")
_HOSTLIKE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_VERSIONISH = re.compile(r"^v?\d+(?:\.\d+)*$")
_HEX = set("0123456789abcdefABCDEF")
#: stands in for the "@" of URL credentials while the other patterns run, so a scrubbed
#: "redacted@host" is not mistaken for an e-mail address and rewritten again
_AT = "\x00"


class Scrubber:
    def __init__(self, salt: bytes | str | None = None, extra_keys: tuple[str, ...] = ()):
        self.salt = salt.encode() if isinstance(salt, str) else (salt or os.urandom(16))
        self.ident_keys = IDENT_KEYS | {k.lower() for k in extra_keys}
        self.literals: dict[str, str] = {}
        self.issued: dict[str, set[str]] = {c: set() for c in ("mac", "uuid", "email", "ip")}
        self.counts: Counter = Counter()
        self._pattern: re.Pattern | None = None

    # ---- pseudonyms ------------------------------------------------------------------
    def _stream(self, label: str, value: str, n: int) -> bytes:
        seed = hmac.new(self.salt, f"{label}\0{value}".encode(), sha256).digest()
        out, block = b"", seed
        while len(out) < n:
            block = sha256(block + seed).digest()
            out += block
        return out[:n]

    def shaped(self, value: str, label: str, hexlike: bool = False) -> str:
        """Same length and character classes as ``value``, derived from it under the salt."""
        bits = self._stream(label, value, len(value))
        upper = any(c.isupper() for c in value) and not any(c.islower() for c in value)
        alphabet = "0123456789ABCDEF" if upper else "0123456789abcdef"
        out = []
        for ch, b in zip(value, bits, strict=True):
            if hexlike and ch in _HEX:
                out.append(alphabet[b % 16])
            elif ch.isdigit():
                out.append(str(b % 10))
            elif ch.isalpha():
                letter = chr(ord("a") + b % 26)
                out.append(letter.upper() if ch.isupper() else letter)
            else:
                out.append(ch)
        result = "".join(out)
        if result == value and any(c.isalnum() for c in value):  # vanishingly rare; never echo
            result = result[:-1] + ("1" if result[-1] != "1" else "2")
        return result

    def ip(self, value: str) -> str:
        try:
            addr = ipaddress.ip_address(value.strip())
        except ValueError:
            return value
        if addr.is_unspecified or addr.is_loopback:
            return value
        b = self._stream("ip", str(addr), 16)
        out = (f"10.{b[0]}.{b[1]}.{b[2] or 1}" if addr.version == 4
               else "fd00::" + ":".join(f"{b[i] << 8 | b[i + 1]:x}" for i in (0, 2, 4)))
        self.issued["ip"].add(out)
        self.counts["ip"] += 1
        return out

    # ---- collecting what is identifying ---------------------------------------------
    def learn(self, value: str, label: str) -> None:
        v = value.strip()
        if len(v) >= MIN_LITERAL and v not in self.literals:
            self.literals[v] = self.shaped(v, label)
            self._pattern = None
            self.counts[label] += 1

    def learn_host(self, value: str) -> None:
        """A hostname, and the domain it sits in: an FQDN's suffix names the site everywhere."""
        v = value.strip().rstrip(".")
        self.learn(v, "hostname")
        parts = v.split(".")
        if len(parts) >= 3:
            self.learn(".".join(parts[1:]), "hostname")

    def collect(self, node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if lk in self.ident_keys and isinstance(v, str):
                    if any(h in lk for h in ("host", "fqdn", "domain")):
                        self.learn_host(v)
                    else:
                        self.learn(v, "mac" if "mac" in lk else "identifier")
                elif any(h in lk for h in HOST_HINTS):
                    for item in v if isinstance(v, list) else [v]:
                        if isinstance(item, str) and _HOSTLIKE.match(item.strip()) \
                                and not _VERSIONISH.match(item.strip()):
                            self.learn_host(item)
                self.collect(v)
        elif isinstance(node, list):
            for v in node:
                self.collect(v)

    def collect_xml(self, text: str) -> None:
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return
        for el in root.iter():
            if el.tag.lower() in self.ident_keys and el.text and el.text.strip():
                self.learn(el.text, "identifier")

    def collect_csv(self, text: str) -> None:
        import csv
        import io
        rows = list(csv.reader(io.StringIO(text.strip()), skipinitialspace=True))
        if len(rows) < 2:
            return
        cols = [re.sub(r"\s*\[.*?\]\s*", "", h).strip().lower() for h in rows[0]]
        for i, name in enumerate(cols):
            if name in self.ident_keys:
                for row in rows[1:]:
                    if i < len(row):
                        self.learn(row[i], "identifier")

    # ---- rewriting -------------------------------------------------------------------
    def _literal_pattern(self) -> re.Pattern | None:
        if self._pattern is None and self.literals:
            alt = "|".join(re.escape(k) for k in sorted(self.literals, key=len, reverse=True))
            self._pattern = re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])")
        return self._pattern

    def text(self, s: str) -> str:
        """Rewrite every identifying thing inside one string."""
        def url(m: re.Match) -> str:
            host = m.group("host")
            bare = host.strip("[]")
            new = self.ip(bare) if _is_ip(bare) else self.shaped(host, "host")
            if new != bare:
                self.counts["url_host"] += 1
            if _is_ip(bare) and host.startswith("["):
                new = f"[{new}]"
            auth = f"redacted{_AT}" if m.group("auth") else ""
            if auth:
                self.counts["secret"] += 1
            return m.group("scheme") + auth + new

        s = _URL.sub(url, s)
        pat = self._literal_pattern()
        if pat:
            s = pat.sub(lambda m: self.literals[m.group(0)], s)
        s = _EMAIL.sub(lambda m: self._issue("email", m.group(0)), s)
        s = _UUID.sub(lambda m: self._issue("uuid", m.group(0), hexlike=True), s)
        s = _MAC.sub(lambda m: self._issue("mac", m.group(0), hexlike=True), s)
        return s.replace(_AT, "@")

    def _issue(self, category: str, value: str, hexlike: bool = False) -> str:
        if value in self.issued[category]:
            return value  # already a stand-in we made
        out = (f"user-{self.shaped('abcdef', 'email' + value)}@example.invalid"
               if category == "email" else self.shaped(value, category, hexlike))
        self.issued[category].add(out)
        self.counts[category] += 1
        return out

    def transform(self, node, key: str = ""):
        lk = key.lower()
        if isinstance(node, dict):
            return {self.text(k) if isinstance(k, str) else k: self.transform(v, str(k))
                    for k, v in node.items()}
        if isinstance(node, list):
            return [self.transform(v, key) for v in node]
        if not isinstance(node, str):
            if any(h in lk for h in SECRET_HINTS) and node not in (None, "", False):
                self.counts["secret"] += 1
                return "[REDACTED]"
            return node
        if any(h in lk for h in SECRET_HINTS):
            if node:
                self.counts["secret"] += 1
            return "[REDACTED]" if node else node
        if lk in PROTECTED_KEYS or "version" in lk:
            return node
        if any(h in lk for h in ADDRESS_HINTS) and _is_ip(node.strip()):
            return self.ip(node)
        return self.text(node)

    def plain(self, text: str) -> str:
        """Rewrite a non-JSON text (nvidia-smi CSV or XML) as a whole."""
        return self.text(text)

    # ---- the safety net --------------------------------------------------------------
    def leaks(self, outputs: dict[str, str]) -> dict:
        """Check what is about to be shared. ``residual`` entries mean the scrubber failed."""
        residual, possible = [], []
        pat = re.compile("|".join(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])"
                                  for k in sorted(self.literals, key=len, reverse=True))
                         ) if self.literals else None
        for name, text in outputs.items():
            if pat:
                residual += [f"{name}: an original identifier is still present"
                             for _ in pat.finditer(text)][:3]
            for m in _EMAIL.finditer(text):
                if m.group(0) not in self.issued["email"]:
                    possible.append(f"{name}: e-mail address {m.group(0)}")
            for m in _MAC.finditer(text):
                if m.group(0) not in self.issued["mac"]:
                    possible.append(f"{name}: MAC address {m.group(0)}")
            for m in _UUID.finditer(text):
                if m.group(0) not in self.issued["uuid"]:
                    possible.append(f"{name}: UUID {m.group(0)}")
            for m in _QUAD.finditer(text):
                q = m.group(0)
                if q not in self.issued["ip"] and not q.startswith(("0.", "127.")):
                    possible.append(f"{name}: {q} (an IP address, or a firmware version?)")
        return {"residual": sorted(set(residual)), "possible": sorted(set(possible))[:40]}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def sniff(text: str) -> str:
    head = text.lstrip()[:1]
    return "json" if head in "{[" else "xml" if head == "<" else "csv"


def sanitize_bundle(files: dict[str, str], salt: bytes | str | None = None,
                    extra_keys: tuple[str, ...] = ()) -> dict:
    """Scrub several captures of one machine together so identifiers correlate across them.

    ``files`` maps a name to its text. Returns ``{"files", "summary", "leaks"}``.
    """
    sc = Scrubber(salt, extra_keys)
    kinds = {n: sniff(t) for n, t in files.items()}
    parsed = {}
    for name, text in files.items():           # first learn everything identifying, from all files
        if kinds[name] == "json":
            parsed[name] = json.loads(text)
            sc.collect(parsed[name])
        elif kinds[name] == "xml":
            sc.collect_xml(text)
        else:
            sc.collect_csv(text)
    out = {}
    for name, text in files.items():           # then rewrite, so a serial seen in one file is
        if kinds[name] == "json":              # replaced in every other
            out[name] = json.dumps(sc.transform(parsed[name]), indent=1, ensure_ascii=False) + "\n"
        else:
            out[name] = sc.plain(text)
    return {"files": out, "summary": dict(sc.counts), "leaks": sc.leaks(out)}
