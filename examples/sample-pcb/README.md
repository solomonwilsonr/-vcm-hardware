# Sample project: sensor_node

A small ESP32-C3 based sensor node used by the VCM demo. Two revisions are provided:

| Revision | Change |
| --- | --- |
| `v1.0/` | 5 V in, AMS1117-3.3 LDO (SOT-223, 1 A) with 22 uF tantalum output capacitor |
| `v1.1/` | U1 swapped to MCP1700-3302E/TT (SOT-23, 250 mA), C2 changed to 10 uF ceramic, R1 nudged 1 mm |

Notes on the files:

- `*.kicad_sch` and `*.kicad_pcb` are hand-authored KiCad 8 S-expression files (generated programmatically,
  not exported from KiCad, and not opened in KiCad by the authors of this sample). The board is placement-only:
  no tracks or zones. The schematic ties pins to nets with global labels at the pin ends.
- The ESP32-C3 module footprint has an abbreviated pad list (8 of the real pads).
- `bom/*.csv` is the bill of materials, `vcm.json` declares firmware links, tests and rails for the dependency graph,
  `firmware/` is a stub, `tests/` holds test procedure notes.
- Component data used by the impact engine is in `services/ai-engine/vcm_ai/data/components.json`;
  it is a small illustrative subset and not a substitute for datasheets.
