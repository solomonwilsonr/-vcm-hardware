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

Current state: a working MVP, 306 passing tests, Appwrite TablesDB live. Trust is in place
(Ed25519 signatures, hash-chained audit log, token auth with roles, policies), VCM can read real
machines (Redfish / `nvidia-smi` captures, drift, measured draw) and reason across a fleet (racks,
recalls, rack power, rollout waves, NetBox placement), and teams can share history through remotes
with locks. What is still demo-grade is the *knowledge*: no catalog entry is datasheet-reviewed,
and the machine captures in `examples/gpu-node/observed/` are synthetic.

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
packages/core            object store, snapshots, audit log, Ed25519 signing,
                         remotes + locks, API tokens                       (vcm_core)
packages/file-parsers    node manifests, observed inventory, KiCad, BOM    (vcm_parsers)
packages/diff-engine     semantic hardware diff, drift                     (vcm_diff)
packages/workflow        releases, rollback, PRs, policies, timeline       (vcm_workflow)
packages/vcm-cli         `vcm` command (+ the only network fetchers)       (vcm_cli)
services/engineering-graph  SQLite / Appwrite dependency graph             (vcm_graph)
services/ai-engine       impact rules, rule plugins, catalog               (vcm_ai)
services/validation      checks, kicad-cli wrapper, evidence               (vcm_validation)
services/traceability    devices, passports, QR, fleet, NetBox import      (vcm_trace)
services/api             FastAPI app + static dashboard                    (vcm_api)
services/mcp-server      MCP server for agents + proposal workflow         (vcm_mcp)
examples/gpu-node        sample node (v1.0 A100 -> v1.1 H100 NVL) + observed/ captures
examples/plugins         example site rule plugin
examples/demo.sh         node demo; demo-pcb.sh is the board demo
integrations/, apps/     documented placeholders — NOT implemented
docs/                    architecture, data model, roadmap, schemas/
```

Each folder holds one importable top-level module. `pyproject.toml` lists every source root twice
(packaging `packages.find` **and** pytest `pythonpath`) — **add new packages to both.**

Dependencies point strictly downwards:
`core → parsers → diff / graph / validation → ai → trace / workflow → mcp → cli / api`. Do not create
upward or sideways imports.

## Hard rules

These are project invariants, not style preferences:

- **No network at runtime, ever, by default.** Anything that calls out must be explicitly opt-in.
  The LLM provider interface ships a no-op default and must stay that way. The only fetchers
  (`vcm inventory fetch-redfish`, `vcm fleet netbox --url`) live in `vcm_cli` and run only when
  those commands are invoked; everything else parses saved captures.
- **Confidence classes are load-bearing.** Every impact finding carries exactly one
  `confidence_class` plus evidence links (`file` with hash, `ref`, `commit`).
  - `verified` — derived from parsed design data, declared project links or stored evidence.
  - `rule_based` — heuristic from engineering rules + the component DB.
  - `speculative` — a prediction, not established by data. An LLM provider may **only** add these.
  - Never label a heuristic `verified`. This is the core trust claim of the product.
  - Rule plugins (`vcm_ai.plugins`) may add `rule_based` / `speculative` only, and a plugin that
    raises must become a finding, never vanish. Keep `RuleContext.add` enforcing this.
- **Trust failures fail closed.** The API refuses to start without a token unless `insecure` is
  explicit; an unsigned or untrusted-key release fails `--require-signature`; a policy violation
  blocks a release unless an override reason is given, and overrides are recorded, never silent.
- **Agents propose, people decide.** Every MCP tool is read-only except `vcm_propose`, which only
  writes a pending record. Do not add an MCP tool that approves, signs, reviews catalog values,
  overrides policy, commits, pushes or locks, and do not add such an action to
  `vcm_mcp.proposals.ACTIONS`: each would let an agent manufacture trust or act unreviewed.
  `tests/test_mcp.py` fails if a tool name contains one of those verbs, if a read tool changes
  repository state, or if an agent identity (`agent:` prefix) or the proposer can decide.
- **Only a named person earns `reviewed`.** `vcm_ai.review.apply_worksheet` validates every row
  first and applies none if any is wrong; it refuses an agent as reviewer, a missing source, and an
  `ok` for a value that changed since the sheet was written. Do not add a path that records a
  review without a person and a citation, and do not expose one to MCP.
- **No merge.** Remotes are fast-forward only; differing tags and records are reported as
  conflicts and never overwritten. Do not add a merge for design files.
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
redundancy policy, chassis cooling, PCIe generation and lane budget, driver/CUDA/BIOS/BMC minimums,
power connector change, DDR generation, DIMM mixing and channel population, socket fit and mixed
CPUs, mixed accelerators on one fabric, NIC vs declared fabric speed (`meta["fabrics"]`), slot
count, and `observed_rules` (drift and measured draw from evidence attached to the base). Every
value a rule uses should be cited with `cite(component, part, keys)` so the evidence says, field by
field, where it came from. `engine.analyze` picks board or node rules via `is_node_design(design)`,
then runs plugins.

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
  `services/ai-engine/vcm_ai/catalog.py` loads `data/catalog-starter.json`, then
  `data/catalog-datacenter.json`, then `data/components.json` (4 curated demo entries, which win), then `~/.vcm/catalog/`, then
  `vcm-catalog/` next to the design. Later layers win on a shared primary `match` key, and lookup is
  longest-prefix so `MCP1700-3302` beats a generic `MCP1700`. Rules must call
  `b.catalog.lookup(c)`, not the module-level `lookup()`, or a project override is silently ignored.
- **Nothing in the bundled catalog is datasheet-reviewed.** Every entry carries
  `reviewed: false` plus a `source`, and `rth_ja_c_per_w` is a package-typical estimate, not part
  data. Rule findings cite this provenance in their `db` evidence link. Do not mark an entry
  `reviewed: true` unless a human actually checked it against the datasheet.
- **The sample KiCad files are hand-written and have never been opened in KiCad.** The parser is
  proven against assumed output, not real output.
- **The API fails closed.** `create_app` raises `AuthConfigError` with no token configured, so
  tests build it with `insecure=True` unless they are testing auth (`tests/test_api_auth.py`).
  With tokens, authors and approvers are the token's user and self-approval is refused; in
  insecure mode they are still self-asserted strings.
- **Signatures are purpose-bound and exclude placement.** A signature covers
  `vcm-sig/1:<purpose>:<digest>`; release digests exclude `manifest_sha256` and `signatures`;
  device digests exclude `signatures` and `location`, so moving a unit between racks does not
  invalidate its passport. Any new device field *is* signed unless added to `_UNSIGNED`.
- **Every state change appends to the audit log** (`repo.audit.append`). New commands that change
  state should too; `vcm audit verify` and the property tests assume nothing edits it in place.
- **Drift compares only what a source covers, and coverage is computed, not assumed.**
  `observed["covers"]` is built from what the capture actually returned: components of that type,
  or a collection fully captured (even if empty). A BMC that does not expose memory must not make
  every DIMM look missing. Never report a type outside `covers` as missing.
- **A parser that is quiet about what it skipped makes a gap look clean.** `vcm inventory doctor`
  (`vcm_parsers.diagnose`) is the answer: fetch errors, kinds present but unread, uncovered types,
  unidentifiable parts. When you teach a parser a new Redfish kind, add it to `RECOGNISED_KINDS`.
- **Real captures go through `vcm inventory sanitize` and are sealed.** `tests/captures/<name>/` is
  picked up by `tests/test_captures.py` automatically; `vcm_parsers.captures.check` fails an
  unsealed, edited-after-sealing, or sensitive folder. The scrubber leaves model names, part
  numbers and firmware versions alone and never rewrites bare dotted numbers in prose (a firmware
  version looks like an IP); it reports those as possible leaks. Do not weaken that to hide noise. Matching runs in passes from strict
  to loose (`vcm_diff/drift.py`): the property tests caught greedy matching inventing mismatches.
- **`summary.trust` is a floor, not a census.** It counts catalog values findings cite as evidence
  (via `cite()`); rules that use an input without citing it (the power budget's per-part wattages)
  are not counted. Keep the wording that says so, and cite values in new rules.
- **The claim checker never takes a report from the caller.** `vcm_check_claims` recomputes
  `analyze` from the repository, so there is nowhere to pass a fabricated one. Completeness (every
  critical finding mentioned) is part of `honest`.
- **Evals grade honesty, mechanically, and have not met a live model.** The reference answerer must
  pass all of `vcm_mcp.evals.CASES` and every flawed built-in must fail exactly its own cases
  (`tests/test_claims_evals.py` pins both, and CI runs `vcm eval run` and expects `--agent sloppy` to
  fail). When you add a case, add the flawed answerer it exists to catch. The first run of these
  evals found two flaws in the evals themselves (a "quiet" scenario that was not quiet, a regex too
  narrow for the reference's own wording): run new cases against all the answerers before trusting them.
- **The impact report is a published schema** (`vcm-impact/1`,
  `docs/schemas/impact-report.schema.json`). Adding fields is fine; removing or changing one needs
  `vcm-impact/2`. `tests/test_schema_and_properties.py` validates real reports against it.
- **Proposals resolve revisions to ids when made, and re-run `precheck` when approved.** An
  approval acts on what was reviewed, and is refused if it no longer holds. Approving runs the
  action under the approver's name (`_acting_as` borrows `$VCM_AUTHOR`), so the audit log names
  the person. Agent output and repository text reaching an agent are data, not instructions.
- **MCP stdout belongs to the protocol.** Log to stderr only. The server is hand-written JSON-RPC
  (no SDK) to keep the core dependency-free; protocol versions it accepts are in `SUPPORTED`.
- **Policies are read from the revision being judged** (`load_policy(repo, rev)`), not the working
  tree, so the rules that applied to a change are recoverable from history.
- Revision history is **first-parent only**. No merge, no remotes, no locking.
- `kicad-cli` DRC/ERC is wrapped but skipped when absent, and has never run against a real install in CI.

## Known gaps and the agreed fix plan

| # | Gap | Status |
|---|-----|--------|
| 2 | Component DB | **largely done** — 90 parts, layered catalog, CSV import, coverage disclosure, per-field attribution and `vcm catalog review`. Remaining: actual datasheet reviews by humans |
| 5 | No release signatures | **done** — pure-Python Ed25519, `trustdb.json`, signed releases and passports, audit head anchored in releases |
| 6 | No API auth | **done** — hashed tokens with roles, fail-closed startup, identity-bound authorship, no self-approval, `vcm serve` on 127.0.0.1 |
| 3 | Hand-made KiCad fixtures | open (needs a real KiCad install) |
| 1 | Schematic nets not traced | open |
| 4 | Single-user, no remotes | **done for the agreed scope** — directory remotes, fast-forward push/pull, records and evidence sync, locks. No S3/Git transport yet; merge deliberately not planned |
| 7 | Manifests drift from machines | **done for captures** — Redfish and `nvidia-smi` parsing, `vcm drift`, measured draw in reports. Live Redfish/NetBox fetchers untested against real services |
| 8 | Single-node view only | **done** — racks, fleet query/recall, rack power, rollout waves, NetBox placement import |

**Recommended next:** real captures from a real BMC and NetBox (scrub with `vcm inventory
sanitize`, drop into `tests/captures/`); human datasheet reviews of the parts your units use most
(`vcm catalog priority`, `worksheet`, `review --from-csv`); run `vcm eval` against a live agent;
then 3 (real KiCad fixtures), which de-risks 1.

### 2. Component catalog — mostly built

Done: `Catalog` with layered sources and longest-prefix lookup; 52-part starter catalog;
`vcm catalog list | show | coverage | import` (JSON and CSV); per-part `source` and `reviewed`
provenance surfaced in `db` evidence links; a `catalog` block on every impact report.

**The disclosure rule — do not regress this.** When a changed component has no catalog entry, the
report emits a `verified`/`warning` finding saying so explicitly, plus a coverage summary. Before
this existed, an unknown part produced a clean-looking report with zero rule-based findings, and a
reader could not distinguish "the rules passed" from "no rules ran". `tests/test_catalog.py::
test_unknown_part_is_disclosed_not_silently_skipped` locks it in.

Per-value attribution is done: a part's `sources` maps a field to a citation string or to
`{"source", "verified_by", "verified_at"}`, and `review_part` / `vcm catalog review` write the
latter. `reviewed` is derived: true only when every field in `REVIEWABLE` that the part carries
has a `verified_by`. Still open: humans actually doing those reviews.

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

### 4. Single-user — reframed, and built that way

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
are README stubs. Remotes have no network transport (a shared directory only). The live Redfish
and NetBox fetchers have never been run against real services. The MCP server has been driven only by
tests and a scripted client, never a live agent; proposals have no dashboard page. "GPU Git" for GPUOS/VACE (`docs/gpu-git.md`) is a scope document only. Hardware
pull requests are **partial**: a local review record with no merge action, identity or permissions.
