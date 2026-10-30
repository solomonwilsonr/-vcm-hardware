# Data model

All formats are plain JSON. Hashes are lowercase hex SHA-256.

## Repository layout

```
.vcm/
  HEAD                      "ref: refs/heads/main" or a detached revision id
  refs/heads/<branch>       revision id
  refs/tags/<tag>           revision id
  objects/ab/cdef...        blobs and revision objects (read-only)
  evidence/<rev>/<kind>.json  list of {version, hash, created_at}; data is an object
  releases/<name>.json      release manifest
  devices/<serial>.json     device record (+ <serial>.svg QR)
  prs/<id>.json             pull request record
  graph.db                  SQLite dependency graph
```

## Revision (snapshot)

The revision id is the SHA-256 of this canonical JSON (sorted keys, compact separators), which is also stored as an object:

```json
{
  "type": "revision",
  "parents": ["<id>"],
  "message": "v1.1 swap regulator",
  "author": "bob",
  "timestamp": "2026-02-10T10:00:00Z",
  "branch": "main",
  "artifacts": [
    {"path": "sensor_node.kicad_pcb", "category": "pcb", "hash": "...", "size": 12345}
  ]
}
```

Categories: `pcb`, `mechanical`, `electronics`, `fpga`, `firmware`, `manufacturing`, `simulation`, `docs`, `other`. A top-level directory named after a category wins; otherwise the file extension decides (`.kicad_pcb` pcb, `.kicad_sch` and BOM CSVs electronics, `.step/.stl` mechanical, `.c/.h/.hex` firmware, `.gbr/.drl` manufacturing, ...). See `packages/core/vcm_core/categories.py`.

Resolution of revision specs: `HEAD`, `HEAD~N`, branch, tag, full id, or unique id prefix. Log follows first parents only.

## Design model (parsed, in memory)

`Component`: `ref`, `value`, `footprint`, `lib_id`, `mpn`, `manufacturer`, `voltage`, placement (`x`, `y`, `rotation`, `layer`), `pads` (pad number to net name), and `by_source` raw values for mismatch checks. Merge priority: value from schematic, footprint from PCB, MPN/manufacturer/voltage from BOM.

## Diff result

`summary` counts, plus `bom` (`added`, `removed`, `replaced`, `value_changed`, `count_changes`), `pcb` (`footprint_changes`, `moved`), `nets` (`added`, `removed`, `changed` with `pins_added`/`pins_removed`) and `changed_refs`. "Replaced" means MPN or library id changed; "value_changed" means only the value changed.

## Impact finding

```json
{
  "id": "R001",
  "confidence_class": "rule_based",
  "severity": "critical",
  "title": "U1 cannot supply peak load on +3V3",
  "detail": "...",
  "refs": ["U1", "U2"],
  "evidence": [
    {"type": "ref", "ref": "U1"},
    {"type": "file", "path": "sensor_node.kicad_pcb", "hash": "...", "revision": "..."},
    {"type": "db", "part": "MCP1700-3302"},
    {"type": "commit", "revision": "...", "role": "head"}
  ],
  "derived_from": ["R001"]
}
```

`confidence_class` is exactly one of `verified`, `rule_based`, `speculative`. `severity` is `info`, `warning` or `critical`. The report also has `summary`, `required_retests` (each with its own class and reason) and `diff_summary`.

## Validation evidence

```json
{"revision": "...", "tool": "vcm-builtin", "status": "pass", "counts": {"error": 0, "warning": 0},
 "issues": [{"code": "MISSING_VALUE", "severity": "error", "message": "...", "ref": "R1", "file": ""}],
 "kicad_cli": null}
```

Each run appends a new numbered entry to `evidence/<rev>/validation.json`; older runs stay available. Codes: `DUPLICATE_REF`, `MISSING_VALUE`, `MISSING_FOOTPRINT`, `SOURCE_MISMATCH`, `MISSING_IN_SOURCE`, `UNCONNECTED_NET`, `EMPTY_NET`, `UNCONNECTED_PAD`, `QTY_MISMATCH` (BOM-only).

## Release manifest (`vcm-release/1`)

```json
{
  "schema": "vcm-release/1", "name": "R1.1", "created_at": "...",
  "revision": "<id>", "parents": ["<id>"], "revision_message": "...",
  "artifacts": {"firmware/main.c": {"hash": "...", "size": 120, "category": "firmware"}},
  "toolchain": {"vcm": "0.1.0", "python": "3.12.1", "platform": "...", "kicad_cli": "not installed"},
  "validation": {"status": "pass", "counts": {"error": 0, "warning": 0}, "evidence_hash": "...", "kicad_cli": "not run"},
  "manifest_sha256": "<hash of the manifest without this field>"
}
```

`vcm release verify` checks: manifest self-hash, revision object, equality of manifest artifacts with the revision snapshot, every artifact blob re-hashed, and with `--worktree` the working files.

## Device record and passport

Record: `serial`, `release`, `revision`, `batch`, `notes`, `registered_at`, `qr_payload` (`$VCM_BASE_URL/<serial>`, default `https://vcm.example.com/device/<serial>`). The passport adds release info, revision with tags, lineage (root to revision), the BOM at that revision, firmware artifact hashes, latest validation status and the QR SVG.

## Pull request record

`id`, `title`, `state` (`open`/`closed`), `author`, `base`, `head`, `diff`, `impact`, `validation` (comparison), `comments`, `approvals`. The computed `status` has approvals, critical finding ids, validation regression and `ready_for_merge`. There is no merge operation yet.

## Dependency graph (SQLite)

Tables `nodes(rev, id, type, name, props)` and `edges(rev, src, dst, type, props)`.

| Node id | Type |
|---|---|
| `component:U1`, `net:+3V3`, `footprint:<lib:name>`, `artifact:<path>`, `firmware:<path>`, `test:<name>`, `device:<serial>`, `release:<name>`, `revision:<id>` | component, net, footprint, artifact, firmware, test, device, release, revision |

| Edge | Meaning |
|---|---|
| `connects_to` | component to net (pad in props) |
| `placed_on` | component to footprint |
| `configured_by` | component to firmware (from `vcm.json` `firmware_links`) |
| `validated_by` | component to test (from `vcm.json` `tests`) |
| `built_from` | device to release, release to revision |
| `defined_in` | component or firmware to the artifact that defines it |
| `part_of` | component to revision |

## `vcm.json` (project knowledge)

```json
{
  "rails": {"+5V": 5.0, "+3V3": 3.3},
  "firmware_links": [{"component": "U2", "paths": ["firmware/main.c"]}],
  "tests": [{"name": "power-rail-bringup", "validates": ["U1", "C2"], "evidence": "tests/power_rail_test.md"}],
  "external_loads": {"J2": {"typ_ma": 10, "peak_ma": 25}}
}
```
