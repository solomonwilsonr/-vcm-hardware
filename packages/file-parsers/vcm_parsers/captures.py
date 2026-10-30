"""Capture folders: real machine captures that become permanent regression tests.

A folder holds scrubbed captures plus ``expect.json`` describing what VCM must make of them::

    tests/captures/<name>/
        redfish.json            a Redfish walk (optional)
        nvidia-smi.csv|.xml     GPU inventory (optional)
        netbox.json             saved NetBox devices (optional, not parsed here)
        expect.json             counts, coverage, firmware keys, allowed problems, the seal

Two guards keep real data out of the repository:

* a folder must be **sealed**: ``expect.json`` carries the sha256 of every capture file as
  ``vcm inventory sanitize`` wrote it, so a raw file pasted in, or a scrubbed one edited later,
  fails until a person re-reads it and runs ``vcm inventory seal``;
* regardless of the seal, nothing that the scrubber always removes may be present: e-mail
  addresses, URL credentials, or an un-redacted secret.

Folders marked ``"synthetic": true`` (hand-written samples) are exempt from the seal.
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from .diagnose import doctor
from .sanitize import _EMAIL, _URL, SECRET_HINTS

SCHEMA = "vcm-capture-expect/1"


def _digest(p: Path) -> str:
    return sha256(p.read_bytes()).hexdigest()


def find_files(folder: Path) -> dict[str, Path]:
    """Classify the capture files in a folder: redfish, nvidia_smi, netbox."""
    out: dict[str, Path] = {}
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name == "expect.json" or p.name.startswith("."):
            continue
        head = p.read_text(errors="replace").lstrip()[:1]
        if p.suffix == ".json":
            try:
                data = json.loads(p.read_text())
            except ValueError:
                continue
            if isinstance(data, dict) and "results" in data and "resources" not in data:
                out.setdefault("netbox", p)
            elif isinstance(data, dict) and ("resources" in data
                                             or any(str(k).startswith("/redfish") for k in data)):
                out.setdefault("redfish", p)
        elif p.suffix in (".csv", ".xml") or head == "<":
            out.setdefault("nvidia_smi", p)
    return out


def seal(folder: str | Path, summary: dict | None = None) -> dict:
    """Record the checksum of every capture file. Run after a person has read the files."""
    folder = Path(folder)
    path = folder / "expect.json"
    expect = json.loads(path.read_text()) if path.is_file() else {"schema": SCHEMA}
    files = sorted(p for p in folder.iterdir()
                   if p.is_file() and p.name != "expect.json" and not p.name.startswith("."))
    old = (expect.get("sanitized") or {}).get("summary", {})
    expect["sanitized"] = {"sha256": {p.name: _digest(p) for p in files},
                           "summary": summary if summary is not None else old}
    path.write_text(json.dumps(expect, indent=1) + "\n")
    return expect


def _walk(node, key=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, str(k))
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    else:
        yield key, node


def _content_problems(name: str, text: str) -> list[str]:
    out = []
    for m in _EMAIL.finditer(text):
        if not m.group(0).endswith("@example.invalid"):
            out.append(f"{name}: contains an e-mail address ({m.group(0)})")
    for m in _URL.finditer(text):
        auth = m.group("auth")
        if auth and auth != "redacted@":
            out.append(f"{name}: a URL carries credentials")
    try:
        data = json.loads(text)
    except ValueError:
        return out
    for key, value in _walk(data):
        if any(h in key.lower() for h in SECRET_HINTS) and value not in (None, "", False,
                                                                         "[REDACTED]"):
            out.append(f"{name}: key {key!r} holds a value that was not redacted")
    return out


def check(folder: str | Path) -> list[str]:
    """Everything wrong with a capture folder; an empty list means it is safe and passing."""
    folder = Path(folder)
    problems: list[str] = []
    path = folder / "expect.json"
    if not path.is_file():
        return [f"{folder.name}: no expect.json (run `vcm inventory sanitize`, or write one)"]
    expect = json.loads(path.read_text())
    files = find_files(folder)
    if not files:
        return [f"{folder.name}: no capture files found"]

    if not expect.get("synthetic"):
        seal_info = (expect.get("sanitized") or {}).get("sha256")
        if not seal_info:
            problems.append(f"{folder.name}: not sealed. Capture it through `vcm inventory "
                            "sanitize`, read the result, then `vcm inventory seal`")
        else:
            on_disk = {p.name for p in folder.iterdir()
                       if p.is_file() and p.name != "expect.json" and not p.name.startswith(".")}
            for name in sorted(on_disk - set(seal_info)):
                problems.append(f"{folder.name}/{name}: not in the seal (added after scrubbing?)")
            for name, digest in sorted(seal_info.items()):
                f = folder / name
                if not f.is_file():
                    problems.append(f"{folder.name}/{name}: sealed but missing")
                elif _digest(f) != digest:
                    problems.append(f"{folder.name}/{name}: changed since it was sealed; read it "
                                    "again and run `vcm inventory seal`")
        for p in files.values():
            problems += _content_problems(f"{folder.name}/{p.name}", p.read_text())

    red = files["redfish"].read_text() if "redfish" in files else None
    nv = files["nvidia_smi"].read_text() if "nvidia_smi" in files else None
    if red is None and nv is None:
        return problems
    res = doctor(red, nv)
    allowed = set(expect.get("allow_problems", []))
    for p in res["problems"]:
        if p not in allowed:
            problems.append(f"{folder.name}: doctor problem not in allow_problems: {p}")
    if "components" in expect and res["components"] != expect["components"]:
        problems.append(f"{folder.name}: components read {res['components']}, "
                        f"expected {expect['components']}")
    if "covers" in expect and res["covers"] != sorted(expect["covers"]):
        problems.append(f"{folder.name}: covers {res['covers']}, expected "
                        f"{sorted(expect['covers'])}")
    if "firmware" in expect and res["firmware"] != sorted(expect["firmware"]):
        problems.append(f"{folder.name}: firmware keys {res['firmware']}, expected "
                        f"{sorted(expect['firmware'])}")
    return problems


def discover(*roots: str | Path) -> list[Path]:
    """Capture folders under the given roots (or the roots themselves if they hold captures)."""
    found = []
    for root in map(Path, roots):
        if not root.is_dir():
            continue
        if (root / "expect.json").is_file():
            found.append(root)
        found += sorted(p for p in root.iterdir() if p.is_dir() and (p / "expect.json").is_file())
    return found

