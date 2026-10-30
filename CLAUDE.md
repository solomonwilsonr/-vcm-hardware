# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**VCM ("Version Control for Hardware")** — an engineering-aware version-control and intelligence
layer for **datacenter node and rack hardware**. Local-first, Python 3.11+, Apache-2.0.
It answers three questions:

1. **What changed?** Semantic diff of the node inventory — accelerators, CPUs, memory, NICs, power
   supplies, slots and shared domains — not byte diffs.
2. **What else is affected?** Dependency graph + offline impact report, every finding labelled
   `verified` / `rule_based` / `speculative` with evidence links.
3. **Which physical units were built from it?** Hash-pinned releases, unit registry, passports, QR.

**Domain history — read this before assuming anything.** VCM began as a PCB tool (KiCad + BOM) and
was pivoted to datacenter node hardware. The pivot replaced the *input layer, catalog, rules,
dataset, demo and docs*; it did **not** touch the core, graph, diff, releases, traceability or
confidence classes, which were already domain-agnostic. The board domain still works and is still
tested (`examples/demo-pcb.sh`, `tests/test_impact.py`) — it is simply no longer the product.
Do not reintroduce PCB vocabulary into user-facing strings.

Current state: a working MVP, 141 passing tests, Appwrite TablesDB live. The mechanism is built end
to end; the *knowledge* (catalog) and *trust* (no signatures, no auth) are still demo-grade.

## Commands

```bash
python3 -m venv .venv && . .venv/bin/activate
make install     # pip install -e ".[dev]"
make test        # pytest -q
make lint        # ruff check .
make demo        # bash examples/demo.sh — full end-to-end flow
make clean
```

Run `make lint test` before any commit. CI runs lint + tests on Python 3.11/3.12/3.13, then the demo
and `vcm bom validate` on both sample BOMs.

## Layout and dependency direction

```
packages/core            object store, snapshots, repository      (vcm_core)
packages/file-parsers    node manifests, KiCad, BOM                (vcm_parsers)
packages/diff-engine     semantic hardware diff                    (vcm_diff)
packages/workflow        releases, rollback, PRs, timeline         (vcm_workflow)
packages/vcm-cli         `vcm` command                             (vcm_cli)
services/engineering-graph  SQLite dependency graph                (vcm_graph)
services/ai-engine       offline impact analysis                   (vcm_ai)
services/validation      checks, kicad-cli wrapper, evidence       (vcm_validation)
services/traceability    devices, passports, QR                    (vcm_trace)
services/api             FastAPI app + static dashboard            (vcm_api)
examples/gpu-node        sample node (v1.0 A100 -> v1.1 H100 NVL)
examples/demo.sh         node demo; demo-pcb.sh is the board demo
integrations/, apps/     documented placeholders — NOT implemented
docs/                    architecture, data model, roadmap, GPU Git scope
```

Each folder holds one importable top-level module. `pyproject.toml` lists every source root twice
(packaging `packages.find` **and** pytest `pythonpath`) — **add new packages to both.**

Dependencies point strictly downwards:
`core → parsers → diff / graph / validation → ai → trace / workflow → cli / api`. Do not create
upward or sideways imports.

## Hard rules

These are project invariants, not style preferences:

- **No network at runtime, ever, by default.** Anything that calls out must be explicitly opt-in.
  The LLM provider interface ships a no-op default and must stay that way.
- **Confidence classes are load-bearing.** Every impact finding carries exactly one
  `confidence_class` plus evidence links (`file` with hash, `ref`, `commit`).
  - `verified` — derived from parsed design data, declared project links or stored evidence.
  - `rule_based` — heuristic from engineering rules + the component DB.
  - `speculative` — a prediction, not established by data. An LLM provider may **only** add these.
  - Never label a heuristic `verified`. This is the core trust claim of the product.
- **Docs stay honest.** If something is not implemented, mark it *Planned*. Update the
  implemented-vs-planned table in `README.md` whenever scope changes. The project's credibility rests
  on that table being true.
- **A test with every behaviour change**, and tests must pass offline.
- Core packages use the standard library only. FastAPI/httpx are for `services/api` and its tests.
- Keep functions small and typed; docstrings where they explain something non-obvious.
- ruff: line-length 100, `select = ["E", "F", "I", "B", "UP"]`, `ignore = ["E501"]`.
- Short imperative commit subjects.

## The node domain

A node is a `*.node.json` manifest (`vcm_parsers/node.py`). Components declare `connections`
(port -> domain), which become `Component.pads`, so **a power domain is a supply rail and the
existing graph/diff/impact engine works unchanged**. `slot / form_factor` becomes `footprint`, so
the footprint-change finding means "physical rework" in both domains. Undeclared keys land in
`Component.props` and are available to rules. Design-level data (chassis, power domains, firmware
versions) lands in `Design.meta["nodes"]`.

Node rules live in `services/ai-engine/vcm_ai/rules_datacenter.py`: power budget under the declared
redundancy policy, chassis cooling, PCIe generation and lane budget, driver minimums, power
connector change. `engine.analyze` picks board or node rules via `is_node_design(design)`.

**Values resolve manifest-first, then catalog** (`rules_datacenter.prop`). The manifest describes
this node as built; the catalog describes the part in general. This is why an unknown part can
still get power findings — and why the disclosure text must say exactly which checks were skipped
rather than claiming none ran.

## Things that will bite you

- **Globally shared domains are not followed for blast radius.** Every component sits on
  `PSU_BANK`, exactly as every component sits on `GND` on a board, so following it would report the
  whole node as affected by any change. `vcm_graph.graph.global_nets()` marks declared power
  domains as ground-like. If blast radius suddenly includes every DIMM, this is why.
- **Identical components must produce one finding, not N.** An 8-GPU node otherwise emits eight
  identical paragraphs. Replacements, connection sets, PCIe downshifts, connector changes and
  catalog disclosures are all grouped. Keep it that way when adding rules.
- **Nets come from PCB pads in the board domain**, not the schematic. A board with a schematic but
  no laid-out PCB yields no connectivity at all.
- **`manifest_sha256` is computed over canonical JSON with that field excluded**
  (`_manifest_hash` in `packages/workflow/vcm_workflow/release.py`). Any new manifest field is
  automatically covered — and is the right hook for detached signatures.
- **Reference resolution in KiCad 7+ lives in `instances`**, not the `Reference` property;
  `parse_schematic` overrides the property with it. Power symbols (`#`-prefixed refs) are skipped.
- **The catalog is layered, and lookups go through it, never a module global.**
  `services/ai-engine/vcm_ai/catalog.py` loads `data/catalog-starter.json` (52 parts) then
  `data/components.json` (4 curated demo entries, which win), then `~/.vcm/catalog/`, then
  `vcm-catalog/` next to the design. Later layers win on a shared primary `match` key, and lookup is
  longest-prefix so `MCP1700-3302` beats a generic `MCP1700`. Rules must call
  `b.catalog.lookup(c)`, not the module-level `lookup()`, or a project override is silently ignored.
- **Nothing in the bundled catalog is datasheet-reviewed.** Every entry carries
  `reviewed: false` plus a `source`, and `rth_ja_c_per_w` is a package-typical estimate, not part
  data. Rule findings cite this provenance in their `db` evidence link. Do not mark an entry
  `reviewed: true` unless a human actually checked it against the datasheet.
- **The sample KiCad files are hand-written and have never been opened in KiCad.** The parser is
  proven against assumed output, not real output.
- **The REST API has no authentication of any kind.** `author` fields are free-text strings
  (`services/api/vcm_api/app.py`), so PR authorship and approvals are self-asserted.
- Revision history is **first-parent only**. No merge, no remotes, no locking.
- `kicad-cli` DRC/ERC is wrapped but skipped when absent, and has never run against a real install in CI.

## Known gaps and the agreed fix plan

Ordered by credibility gained per unit of effort. Items 2, 5, 6, 3 are contained; 1 and 4 are real
engineering.

| # | Gap | Effort | Status |
|---|-----|--------|--------|
| 2 | Component DB has 4 parts | ~1–2 days | **largely done** — 54 parts, layered catalog, CSV import, coverage disclosure. Remaining: per-value attribution, and getting entries actually datasheet-reviewed |
| 5 | No release signatures | ~1–2 days | open |
| 6 | No API auth | ~1 day | open |
| 3 | Hand-made KiCad fixtures | ~2–3 days | open |
| 1 | Schematic nets not traced | ~1–2 weeks | open |
| 4 | Single-user, no remotes | ~2–3 weeks | **partly addressed** — the graph now has a shared Appwrite TablesDB backend (`vcm_graph.backends`). Devices and releases are still local JSON. Merge is still deliberately not planned |

**Recommended order: 6, then 5, then 3 — 3 de-risks 1 by supplying real files.**

### 2. Component catalog — mostly built

Done: `Catalog` with layered sources and longest-prefix lookup; 52-part starter catalog;
`vcm catalog list | show | coverage | import` (JSON and CSV); per-part `source` and `reviewed`
provenance surfaced in `db` evidence links; a `catalog` block on every impact report.

**The disclosure rule — do not regress this.** When a changed component has no catalog entry, the
report emits a `verified`/`warning` finding saying so explicitly, plus a coverage summary. Before
this existed, an unknown part produced a clean-looking report with zero rule-based findings, and a
reader could not distinguish "the rules passed" from "no rules ran". `tests/test_catalog.py::
test_unknown_part_is_disclosed_not_silently_skipped` locks it in.

Still open: per-value attribution (each field carrying its own `{"value", "source", "verified_by"}`
rather than one `source` per part), and getting entries genuinely datasheet-reviewed so
`reviewed: true` means something.

### 5. Release signing

`manifest_sha256` is already the right digest to sign. Add `vcm key gen`, `vcm release sign <name>`,
and a `signature` check inside `verify_release` alongside the existing ones. Ed25519 — optional
`cryptography` dependency with a clear error when absent (or ~80 lines of pure-Python RFC 8032 to
preserve the zero-dependency core). Public keys in a committed `trustdb.json` so verification is
offline and the trust set is itself version-controlled. Sign device passports with the same key.

Today `release verify` only re-hashes: it catches accidental and naive tampering, but anyone who can
rewrite both the manifest and the object store can forge a release. Hashes prove consistency, not
authenticity.

### 6. API authentication

Bearer-token middleware, token from `VCM_API_TOKEN`. **Fail closed** — refuse to start without one
unless `--insecure` is passed explicitly. Default the uvicorn bind to `127.0.0.1` and document it.
Bind identity to the token so PR authors and approvals stop being self-asserted; this half-fixes
module 9's "no identity or permissions" gap for free.

### 3. Real KiCad fixtures

Build the sample ESP32-C3 board in actual KiCad 9, save it, replace the hand-written fixtures, and
keep the hand-made ones as a separate *synthetic* set so parser bugs stay distinguishable from
fixture bugs. Add fixtures for KiCad 6, 7, 8 and 9 output of the same board — the parser claims 6–9
support, so prove it. Add a KiCad container/apt step to CI so `kicad-cli` DRC/ERC actually runs,
which also retires the untested-wrapper caveat.

### 1. Schematic net tracing

A geometric connectivity pass over `.kicad_sch`:

1. Parse `wire`, `junction`, `label`, `global_label`, `hierarchical_label`, `no_connect`, `bus`,
   `bus_entry`.
2. Build a graph of wire endpoints, union-find them, snap-merge coincident points at junctions.
3. Resolve symbol pin positions — apply each symbol's `at` transform (rotation **and** mirror) to the
   pins in its `lib_symbols` definition, then attach pins to wire nodes at matching coordinates.
   **This is the fiddly part; mirror/rotation bugs are the classic failure.**
4. Name each net: explicit label > global label > power symbol > auto-generated.
5. Walk hierarchical sheets, mapping sheet pins to sub-sheet hierarchical labels.

Extend `Component` with a `pins` field so pinout rules work pre-layout.

**The payoff:** with both sources available, cross-check schematic nets against PCB pad nets and
report disagreements as a `verified` finding. A schematic/layout mismatch is genuinely valuable to
catch and nobody else in the local-first space does it.

### 4. Single-user — reframe rather than fix

**Do not implement three-way merge for PCBs.** Merging two divergent board layouts is not a
well-defined operation, and real hardware teams don't do it — they lock.

- **Sync, yes:** push/pull against a remote object store. Content-addressed blobs make this
  straightforward (S3 / filesystem / Git-backed transport, as `docs/roadmap.md` Phase 3 proposes).
- **Locking instead of merge:** `vcm lock <file>` with holder identity and expiry — what Altium 365
  and PDM systems actually offer, at a fraction of the work.
- **Merge only where meaningful:** BOM CSV and `vcm.json` genuinely three-way merge. For
  `.kicad_pcb`, offer "pick a side, here is the semantic diff between them" — which VCM is uniquely
  equipped to show.
- Identity and permissions fall out of the auth work in gap 6.

## Not implemented — do not describe these as working

KiCad and FreeCAD plugins, the Git bridge, the web app and the desktop app (`integrations/`, `apps/`)
are README stubs. "GPU Git" for GPUOS/VACE (`docs/gpu-git.md`) is a scope document only. Hardware
pull requests are **partial**: a local review record with no merge action, identity or permissions.
