"""Artifact categorisation by directory hint and file extension."""
from __future__ import annotations

from pathlib import PurePosixPath

CATEGORIES = (
    "pcb", "mechanical", "electronics", "fpga",
    "firmware", "manufacturing", "simulation", "docs", "other",
)

_EXT = {
    ".kicad_pcb": "pcb", ".brd": "pcb", ".pcbdoc": "pcb",
    ".kicad_sch": "electronics", ".kicad_pro": "electronics", ".schdoc": "electronics",
    ".step": "mechanical", ".stp": "mechanical", ".stl": "mechanical",
    ".fcstd": "mechanical", ".dxf": "mechanical", ".f3d": "mechanical",
    ".v": "fpga", ".sv": "fpga", ".vhd": "fpga", ".xdc": "fpga", ".sdc": "fpga", ".bit": "fpga",
    ".c": "firmware", ".h": "firmware", ".cpp": "firmware", ".rs": "firmware",
    ".ino": "firmware", ".ld": "firmware", ".hex": "firmware", ".elf": "firmware",
    ".gbr": "manufacturing", ".drl": "manufacturing", ".gtl": "manufacturing",
    ".gbl": "manufacturing", ".pos": "manufacturing",
    ".cir": "simulation", ".asc": "simulation", ".sp": "simulation", ".spice": "simulation",
    ".md": "docs", ".pdf": "docs", ".txt": "docs", ".rst": "docs",
}


def categorize(path: str) -> str:
    """Return the category of a repo-relative POSIX path."""
    p = PurePosixPath(path)
    if len(p.parts) > 1 and p.parts[0].lower() in CATEGORIES:
        return p.parts[0].lower()
    ext = p.suffix.lower()
    if ext == ".csv":
        return "electronics" if "bom" in p.name.lower() else "other"
    if ext == ".json" and p.name == "vcm.json":
        return "docs"
    return _EXT.get(ext, "other")
