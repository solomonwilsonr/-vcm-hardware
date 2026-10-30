"""BOM CSV parsing with tolerant column aliases."""
from __future__ import annotations

import csv
import io
import re

from .model import Component

_ALIASES = {
    "ref": ("reference", "references", "refs", "designator", "ref", "refdes"),
    "value": ("value", "comment", "val"),
    "footprint": ("footprint", "package", "pattern"),
    "mpn": ("mpn", "manufacturer part number", "part number", "manufacturer_part_number"),
    "manufacturer": ("manufacturer", "mfr", "mfg"),
    "description": ("description", "desc"),
    "voltage": ("voltage", "rating", "voltage rating"),
    "qty": ("qty", "quantity"),
    "lib_id": ("lib_id", "symbol"),
}


def _column_map(header: list[str]) -> dict[str, int]:
    low = [h.strip().lower() for h in header]
    out: dict[str, int] = {}
    for key, names in _ALIASES.items():
        for name in names:
            if name in low:
                out[key] = low.index(name)
                break
    return out


def looks_like_bom(text: str) -> bool:
    first = text.lstrip("﻿").splitlines()[:1]
    if not first:
        return False
    cols = _column_map(next(csv.reader(first)))
    return "ref" in cols and "value" in cols


def split_refs(cell: str) -> list[str]:
    return [r for r in re.split(r"[,\s;]+", cell.strip()) if r]


def parse_bom(text: str) -> tuple[list[Component], list[str], list[dict]]:
    """Return (components, duplicate_refs, qty_mismatches). One component per designator."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return [], [], []
    cols = _column_map(rows[0])
    if "ref" not in cols:
        raise ValueError("BOM has no reference/designator column")

    def cell(row: list[str], key: str) -> str:
        i = cols.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    comps: list[Component] = []
    dups: list[str] = []
    mismatches: list[dict] = []
    seen: set[str] = set()
    for row in rows[1:]:
        if not any(c.strip() for c in row):
            continue
        refs = split_refs(cell(row, "ref"))
        qty = cell(row, "qty")
        if qty.isdigit() and int(qty) != len(refs):
            mismatches.append({"refs": refs, "qty": int(qty)})
        for ref in refs:
            if ref in seen:
                dups.append(ref)
                continue
            seen.add(ref)
            comps.append(Component(
                ref=ref, value=cell(row, "value"), footprint=cell(row, "footprint"),
                lib_id=cell(row, "lib_id"), mpn=cell(row, "mpn"),
                manufacturer=cell(row, "manufacturer"), description=cell(row, "description"),
                voltage=cell(row, "voltage")))
    return comps, dups, mismatches
