"""Build a merged Design from the files of one revision."""
from __future__ import annotations

from .bom import looks_like_bom, parse_bom
from .kicad import parse_pcb, parse_schematic
from .model import Component, Design

_FIELDS = ("value", "footprint", "lib_id", "mpn", "manufacturer", "description", "voltage")
# Which source wins for each field (first non-empty in this order).
_PRIORITY = {
    "value": ("schematic", "pcb", "bom"),
    "footprint": ("pcb", "schematic", "bom"),
    "lib_id": ("schematic", "bom", "pcb"),
    "mpn": ("bom", "schematic", "pcb"),
    "manufacturer": ("bom", "schematic", "pcb"),
    "description": ("bom", "schematic", "pcb"),
    "voltage": ("bom", "schematic", "pcb"),
}


def build_design(files: dict[str, bytes]) -> Design:
    design = Design()
    per_source: dict[str, dict[str, Component]] = {"schematic": {}, "pcb": {}, "bom": {}}

    def add(kind: str, path: str, comps: list[Component], dups: list[str]) -> None:
        design.sources.setdefault(kind, []).append(path)
        for d in dups:
            design.duplicates.append({"ref": d, "source": path})
        for c in comps:
            if c.ref in per_source[kind]:
                design.duplicates.append({"ref": c.ref, "source": path})
            else:
                per_source[kind][c.ref] = c

    for path in sorted(files):
        low = path.lower()
        text = files[path].decode("utf-8", errors="replace")
        if low.endswith(".kicad_sch"):
            comps, dups = parse_schematic(text)
            add("schematic", path, comps, dups)
        elif low.endswith(".kicad_pcb"):
            comps, declared, dups = parse_pcb(text)
            design.declared_nets |= declared
            add("pcb", path, comps, dups)
        elif low.endswith(".csv") and looks_like_bom(text):
            comps, dups, _ = parse_bom(text)
            add("bom", path, comps, dups)

    refs = sorted({r for src in per_source.values() for r in src})
    for ref in refs:
        merged = Component(ref=ref)
        for fld in _FIELDS:
            for kind in _PRIORITY[fld]:
                src = per_source[kind].get(ref)
                if src is not None and getattr(src, fld):
                    setattr(merged, fld, getattr(src, fld))
                    break
        for kind, src in per_source.items():
            if ref in src:
                s = src[ref]
                merged.by_source[kind] = {f: getattr(s, f) for f in _FIELDS}
        pcb = per_source["pcb"].get(ref)
        if pcb is not None:
            merged.x, merged.y, merged.rotation = pcb.x, pcb.y, pcb.rotation
            merged.layer, merged.pads = pcb.layer, dict(pcb.pads)
        design.components[ref] = merged
    return design


def design_from_revision(repo, rev) -> Design:
    """Parse the design of a revision (id/spec or Revision) from a Repository."""
    from vcm_core import Revision
    r = rev if isinstance(rev, Revision) else repo.get_revision(rev)
    wanted = {p: repo.store.read(a.hash) for p, a in r.artifacts.items()
              if p.lower().endswith((".kicad_sch", ".kicad_pcb", ".csv"))}
    return build_design(wanted)
