"""KiCad .kicad_sch / .kicad_pcb extraction (KiCad 6-9 S-expression formats)."""
from __future__ import annotations

from .model import Component
from .sexpr import atoms, child, children, head, parse


def _props(node) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in children(node, "property"):
        a = atoms(p)
        if len(a) >= 2:
            out[a[0]] = a[1]
    for t in children(node, "fp_text"):  # KiCad 6/7 footprints
        a = atoms(t)
        if len(a) >= 2 and a[0] in ("reference", "value"):
            out[a[0].capitalize()] = a[1]
    return out


def parse_schematic(text: str) -> tuple[list[Component], list[str]]:
    """Return (components, duplicate_refs) from a schematic. Power symbols are skipped."""
    root = parse(text)
    comps: list[Component] = []
    seen: set[str] = set()
    dups: list[str] = []
    for sym in children(root, "symbol"):  # top-level only: lib_symbols definitions are nested
        lib = child(sym, "lib_id")
        if lib is None:
            continue
        props = _props(sym)
        ref = props.get("Reference", "")
        inst = child(sym, "instances")
        if inst is not None:  # KiCad 7+: authoritative reference lives in instances
            for node in _walk(inst, "reference"):
                a = atoms(node)
                if a:
                    ref = a[0]
        if not ref or ref.startswith("#"):
            continue
        if ref in seen:
            dups.append(ref)
            continue
        seen.add(ref)
        comps.append(Component(
            ref=ref, value=props.get("Value", ""), footprint=props.get("Footprint", ""),
            lib_id=atoms(lib)[0] if atoms(lib) else "", mpn=props.get("MPN", ""),
            manufacturer=props.get("Manufacturer", ""), description=props.get("Description", ""),
            voltage=props.get("Voltage", "")))
    return comps, dups


def _walk(node, name):
    for c in node[1:]:
        if isinstance(c, list):
            if head(c) == name:
                yield c
            yield from _walk(c, name)


def _num(s: str, default: float = 0.0) -> float:
    try:
        return float(s)
    except ValueError:
        return default


def parse_pcb(text: str) -> tuple[list[Component], set[str], list[str]]:
    """Return (components with placement + pad nets, declared net names, duplicate refs)."""
    root = parse(text)
    declared = {atoms(n)[-1] for n in children(root, "net") if atoms(n) and atoms(n)[-1]}
    comps: list[Component] = []
    seen: set[str] = set()
    dups: list[str] = []
    for fp in children(root, "footprint") + children(root, "module"):
        a = atoms(fp)
        props = _props(fp)
        ref = props.get("Reference", "")
        if not ref or ref.startswith("#"):
            continue
        if ref in seen:
            dups.append(ref)
            continue
        seen.add(ref)
        at = atoms(child(fp, "at"))
        layer = atoms(child(fp, "layer"))
        pads: dict[str, str] = {}
        for pad in children(fp, "pad"):
            pa = atoms(pad)
            if not pa:
                continue
            net = atoms(child(pad, "net"))
            pads[pa[0]] = net[-1] if net else ""
        comps.append(Component(
            ref=ref, value=props.get("Value", ""), footprint=a[0] if a else "",
            x=_num(at[0]) if at else None, y=_num(at[1]) if len(at) > 1 else None,
            rotation=_num(at[2]) if len(at) > 2 else 0.0,
            layer=layer[0] if layer else "", pads=pads, mpn=props.get("MPN", "")))
    return comps, declared, dups
