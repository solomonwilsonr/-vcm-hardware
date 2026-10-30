"""Normalised hardware design model produced by the parsers."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Component:
    ref: str
    value: str = ""
    footprint: str = ""
    lib_id: str = ""
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    voltage: str = ""
    x: float | None = None
    y: float | None = None
    rotation: float = 0.0
    layer: str = ""
    pads: dict[str, str] = field(default_factory=dict)  # pad number -> net name ("" = unconnected)
    by_source: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def footprint_name(self) -> str:
        return self.footprint.split(":")[-1]

    def to_dict(self) -> dict:
        return {"ref": self.ref, "value": self.value, "footprint": self.footprint,
                "lib_id": self.lib_id, "mpn": self.mpn, "manufacturer": self.manufacturer,
                "voltage": self.voltage, "x": self.x, "y": self.y,
                "rotation": self.rotation, "layer": self.layer}


@dataclass
class Design:
    components: dict[str, Component] = field(default_factory=dict)
    sources: dict[str, list[str]] = field(default_factory=dict)  # kind -> paths
    duplicates: list[dict] = field(default_factory=list)  # {"ref","source"}
    declared_nets: set[str] = field(default_factory=set)

    @property
    def nets(self) -> dict[str, list[str]]:
        """net name -> sorted ``REF.PAD`` pins, from PCB pad assignments."""
        out: dict[str, list[str]] = {}
        for c in self.components.values():
            for pad, net in c.pads.items():
                if net:
                    out.setdefault(net, []).append(f"{c.ref}.{pad}")
        return {k: sorted(v) for k, v in sorted(out.items())}

    def unconnected_pads(self) -> list[str]:
        return sorted(f"{c.ref}.{p}" for c in self.components.values()
                      for p, n in c.pads.items() if not n)

    def net_of(self, ref: str, pad: str) -> str:
        return self.components[ref].pads.get(pad, "") if ref in self.components else ""
