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

Current state: a working MVP, ~180 passing tests, Appwrite TablesDB live. The mechanism is built end
to end; the *knowledge* (catalog) is still demo-grade. *Trust* now has real primitives (Ed25519
release signatures, API bearer auth) but no roles, revocation or key protection.

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
- **`manifest_sha256` is computed over canonical JSON excluding `manifest_sha256` and `signatures`**
  (`_manifest_hash` / `UNHASHED` in `packages/workflow/vcm_workflow/release.py`). Any new manifest
  field is automatically covered. `signatures` must stay excluded, or signing would change the digest
  being signed.
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
- **API auth is fail-closed.** `create_app` raises without `VCM_API_TOKEN`/`VCM_API_TOKENS` unless
  `insecure=True`; there is deliberately **no module-level `app`**, so serve with
  `python -m vcm_api` (binds 127.0.0.1) or `uvicorn --factory vcm_api.app:create_app`. In
  insecure mode identities are self-asserted free text again. Behind auth, always take the actor
  from `actor(request, claimed)` — never from the request body. There are no roles: any valid token
  may do anything, including `force`-breaking a lock.
- **The CLI is not authenticated.** `--author`/`--by`/`--holder` are self-asserted. Only the API
  binds identity to a token, and only signatures give authenticity.
- Signing: `trustdb.json` (project root, committed) is the trust set; verification reads the
  *working tree* copy. A valid signature from a key not in it is reported as a failed check, not
  silently accepted. Signatures are domain-separated (`release` vs `passport`). Never log or commit
  anything from `~/.vcm/keys` (override: `$VCM_KEY_DIR`).
- Remote sync (`vcm_core/remote.py`): filesystem transport only. Push validates everything (fast-
  forward, tag conflicts, remote locks) **before** writing, re-hashes every blob it copies, and
  syncs `releases/` and `devices/` as create-only records (release signatures are unioned). PRs,
  evidence-index conflicts and S3/HTTP/SSH are not handled.
- Locks (`vcm_core/locks.py`) are advisory and enforced only in `Repository.commit` and `push`.
- `Repository.fast_forward` is the only way a branch moves other than `commit`; it refuses
  divergence and requires a clean tree when the branch is checked out. PR merge and pull use it.
- Revision history is **first-parent only**. No merge.
- `kicad-cli` DRC/ERC is wrapped but skipped when absent, and has never run against a real install in CI.

## Known gaps and the agreed fix plan

Ordered by credibility gained per unit of effort. Items 2, 5, 6, 3 are contained; 1 and 4 are real
engineering.

| # | Gap | Effort | Status |
|---|-----|--------|--------|
| 2 | Component DB has 4 parts | ~1–2 days | **largely done** — 54 parts, layered catalog, CSV import, coverage disclosure. Remaining: per-value attribution, and getting entries actually datasheet-reviewed |
| 5 | No release signatures | ~1–2 days | **done** — pure-Python Ed25519 (`vcm_core/ed25519.py`, RFC 8032 vectors in tests), `vcm key`, `vcm release sign`, `trustdb.json`, signed passports. Open: key revocation/expiry, encrypted private keys |
| 6 | No API auth | ~1 day | **done** — bearer tokens, fail-closed, 127.0.0.1 default, identity bound to token. Open: roles/permissions, TLS, rotation, audit log |
| 3 | Hand-made KiCad fixtures | ~2–3 days | open |
| 1 | Schematic nets not traced | ~1–2 weeks | open |
| 4 | Single-user, no remotes | ~2–3 weeks | **largely addressed** — filesystem remotes (push/pull/clone, fast-forward only), file locks, protected branches, PR merge (fast-forward), plus the shared Appwrite graph backend. Open: S3/HTTP transports, PR sync, required-reviewer lists. Merge is still deliberately not planned |

**Remaining order: 3, then 1 — 3 de-risks 1 by supplying real files.** (6 and 5 are done; 4 is largely done.)

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

### 5. Release signing — built

`vcm key gen <name> [--trust]`, `vcm release sign <name> --key <k>`, `vcm release verify
--require-signature`, `vcm device passport --sign` / `verify-passport`. Signs `manifest_sha256`
(passports: the canonical-JSON digest minus `signatures`) with pure-Python Ed25519. Verification
reads `trustdb.json` and reports `authenticated` separately from `ok`: an unsigned release still
verifies (hashes prove consistency), but is never described as authenticated. Tests:
`tests/test_signing.py`. Open: revocation/expiry, encrypted private keys, signing commits/PRs.

### 6. API authentication — built

`vcm_api/auth.py` + the middleware in `create_app`. Open: roles, TLS, token rotation, audit log.
Tests: `tests/test_api_auth.py`.

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

### 4. Collaboration — built, deliberately not Git

Do **not** implement three-way merge for PCBs: merging two board layouts is not a well-defined
operation, and real hardware teams lock instead.

Built: `vcm remote | push | pull | clone` (filesystem transport, fast-forward only), `vcm lock |
unlock | locks` (local or `--remote`), `vcm protect`, `vcm config required_approvals`, `vcm pr
merge`. Tests: `tests/test_remote.py`, `test_locks.py`, `test_merge.py`.

Open: S3/HTTP/SSH transports (content-addressed blobs make them straightforward); syncing PRs;
required-reviewer lists; merging BOM CSV / `vcm.json` three-way; for `.kicad_pcb` "pick a side, here
is the semantic diff between them". Still not planned: issues, a CI service, a hosted web UI.

## Not implemented — do not describe these as working

KiCad and FreeCAD plugins, the Git bridge, the web app and the desktop app (`integrations/`, `apps/`)
are README stubs. "GPU Git" for GPUOS/VACE (`docs/gpu-git.md`) is a scope document only. Hardware
pull requests are fast-forward only: no three-way merge, no required-reviewer lists, and identity is verified only behind API auth.
