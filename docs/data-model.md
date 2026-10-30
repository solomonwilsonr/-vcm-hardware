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
  "audit_head": "<hash of the newest audit event when the release was cut>",
  "policy": {"present": true, "ok": true, "violations": [], "skipped": []},
  "manifest_sha256": "<hash of the manifest without this field and without signatures>",
  "signatures": [{"algorithm": "ed25519", "key_id": "...", "signer": "...", "purpose": "release", "signed_at": "...", "signature": "..."}]
}
```

`vcm release verify` checks: manifest self-hash, each signature against `trustdb.json`, revision object, equality of manifest artifacts with the revision snapshot, every artifact blob re-hashed, policy rules for the verify stage, with `--require-signature` that a trusted key signed it, and with `--worktree` the working files.

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

## Audit log (`.vcm/audit/events.jsonl`)

One JSON object per line: `seq`, `at`, `actor`, `action` (`commit`, `tag`, `branch`,
`release.create`, `release.sign`, `device.register`, `device.sign`, `device.place`, `observe`,
`pr.open`, `pr.approve`, `policy.override`, `push`, `pull`, `lock.*`, `key.*`, `token.*`,
`rack.define`, `netbox.import`, `catalog.review`), `subject`, `data`, `prev` (hash of the previous
event, or 64 zeros) and `hash` (sha256 of the canonical JSON of the event without `hash`).

## Signatures and trust

`trustdb.json` (`vcm-trust/1`, committed): `keys` maps a key id (first 16 hex of the sha256 of the
public key) to `{name, public_key, added_at, roles}`. A signature is
`{algorithm: "ed25519", key_id, signer, purpose, signed_at, signature}` over the bytes
`vcm-sig/1:<purpose>:<digest>`. Release manifests carry `signatures` (not part of
`manifest_sha256`); device records carry `signatures` over a digest that excludes `signatures` and
`location`.

## Observed inventory (`vcm-observed/1`) and drift (`vcm-drift/1`)

Observation: `node`, `sources`, `covers` (component types the sources can see), `components`
(`type`, `model`, `mpn`, `serial`, `slot`, `props`), `firmware` (`bios`, `bmc`, `gpu_driver`,
`cuda`, `inventory:<name>`), `telemetry` (`node_power_w` list, `gpu_power_w` per index). Drift:
`status` (`in_sync` / `drifted`), `checked_types`, `unchecked_types`, `checked_firmware`, and
`findings` (`kind`: `missing`, `unexpected`, `model_mismatch`, `firmware`), all `verified`.
`vcm drift --attach` stores `{observation, drift, captured_at}` as evidence kind `observed`.

## Racks and placement

`.vcm/racks/<name>.json`: `name`, `power_budget_w`, `cooling_w`, `site`, `units`. A device record
may carry `location: {rack, position}`.

## Tokens (`.vcm/tokens.json`, mode 0600)

`tokens` maps the sha256 of a token to `{user, role, created_at, id}`. The token itself is never
stored.

## Remotes

`.vcm/remotes.json` maps a name to `{url}` (a directory). The remote directory holds `objects/`,
`refs/heads`, `refs/tags`, `records/<kind>/`, `evidence/` and `locks/<sha256(path)[:32]>.json`
(`path`, `holder`, `reason`, `acquired_at`, `expires_at`). Fetched remote heads are kept under
`.vcm/refs/remotes/<name>/`.

## Policy (`vcm-policy.json`, `vcm-policy/1`)

`rules`: each has an `id`, at least one check (`forbid_severity`, `min_catalog_coverage`,
`forbid_unknown_parts`, `require_approvals`, `forbid_validation_regression`,
`require_observed_base`, `require_signed_release`), optional `stages` and optional `when`
(`title_matches`, `severity_at_least`). Release manifests record
`policy: {present, ok, violations, skipped, override?}`.

## Proposals (`.vcm/proposals/P0001.json`)

`id`, `action` (`release.create`, `pr.open`, `fleet.place`, `rack.define`, `device.register`),
`args` (revisions already resolved to ids), `rationale`, `proposer` (`agent:<name>`), `status`
(`pending`, `approved`, `rejected`), `created_at`, `summary`, `preview`; once decided,
`decided_by`, `decided_at`, and `result` or `reason`. Audit actions: `proposal.create` (actor: the
agent), `proposal.approve` and `proposal.reject` (actor: the person), plus the action's own events
under the approver's name.

## Capture folders (`tests/captures/<name>/`)

`redfish.json`, `nvidia-smi.csv|xml`, optional `netbox.json`, and `expect.json`
(`vcm-capture-expect/1`): `components` (counts by type), `covers`, `firmware` (keys found),
`allow_problems` (doctor problems accepted for this machine), `notes`, `synthetic` (hand-written,
exempt from the seal) and `sanitized: {sha256: {file: digest}, summary}` written by
`vcm inventory sanitize`/`seal`. `vcm inventory doctor` returns `vcm-doctor/1`: `ok`, `problems`
(capture incomplete or unusable), `warnings`, `components`, `covers`, `firmware`, `telemetry`.
