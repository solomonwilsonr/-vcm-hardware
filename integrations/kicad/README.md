# KiCad integration

**Status: not implemented (placeholder).**

What exists today: VCM reads KiCad `.kicad_sch` and `.kicad_pcb` files directly (`packages/file-parsers`) and can optionally call `kicad-cli` for DRC/ERC (`services/validation`). There is no KiCad plugin.

Planned: an action plugin that commits the current project, shows the semantic diff and impact report in the editor, and highlights changed components on the PCB.
