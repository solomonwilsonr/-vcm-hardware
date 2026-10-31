# VCM — Version Control for Hardware

**Every hardware change has a blast radius. VCM makes it traceable.**

VCM is an engineering-aware version-control and intelligence layer for **datacenter node and rack
hardware**. You commit a node's configuration the way you commit code; VCM tells you what a change
to it breaks, which physical units are affected, and how much to trust each thing it says.

It is built around three questions:

1. **What exactly changed in this configuration?** A semantic diff of the node inventory —
   accelerators, CPUs, memory, NICs, power supplies, slots and the domains they share — not changed
   bytes in a manifest.
2. **What else could that change affect, and what evidence supports it?** A dependency graph plus an
   offline impact report whose findings are labelled `verified`, `rule_based` or `speculative`, each
   with evidence links back to a file and hash.
3. **Which physical units were built from the affected configuration?** Releases pin artifact
   hashes; nodes are registered against releases and can be looked up by component or revision.

> Built for the Appwrite Databases Hackathon: see [docs/hackathon.md](docs/hackathon.md).

> **Status: working MVP, honest scope.** Everything marked *Implemented* below runs and is covered
> by tests in this repository. Everything marked *Planned* does not exist yet. See
> [Limitations](#limitations).

## The demo in one paragraph

A node runs eight A100 PCIe accelerators on four 2000 W supplies in a 2+2 redundant bank. Someone
proposes swapping them for H100 NVL. The manifest diff is eight replaced line items. What VCM says
is that the node now draws **4190 W against a 4000 W usable budget**, so losing one supply drops it;
that the **installed driver (470.223.02) is below the H100's minimum (525.60.13)**, so the cards
will not initialise at all, and that CUDA 11.4 is below their 12.0 minimum; that the chassis is
rated for 3600 W of cooling; that the Gen5 cards sit on a Gen4 host root; that both PCIe roots are
lane-oversubscribed; and that the power connector changed from 8-pin EPS to 12VHPWR, so the existing
harness does not fit.

None of that is written down anywhere. It falls out of the graph plus a component catalog.

```
$ vcm impact v1.0 v1.1
Findings: verified 6, rule_based 9, speculative 3 | severity: critical 3, warning 9, info 6
```

Then it checks the premise. Fed the machine's own Redfish and `nvidia-smi` output, `vcm drift`
reports that v1.0 was never quite true: a DIMM has failed and the driver actually installed is
older than the one declared. The fleet commands say which racks go over budget if every unit on the
old release is upgraded, and plan the rollout in waves of at most one unit per rack, holding back
every unit whose change still has critical findings.

## Modules: implemented vs planned

| # | Module | Location | Status |
|---|--------|----------|--------|
| 1 | Content-addressed object store, snapshots, branches, tags, commit/log/checkout/diff, evidence store | `packages/core` | **Implemented** |
| 2 | Hash-chained audit log of every state change | `packages/core` (`vcm_core.audit`) | **Implemented** |
| 3 | Ed25519 signing of releases and unit passports, trust database | `packages/core` (`vcm_core.signing`) | **Implemented** in pure Python (RFC 8032 vectors tested) |
| 4 | Remotes (shared directory), push/fetch/pull, file locks | `packages/core` (`vcm_core.remote`) | **Implemented**, fast-forward only by design. No S3 or Git transport |
| 5 | Input parsers: node manifests (`*.node.json`), KiCad `.kicad_sch`/`.kicad_pcb`, BOM CSV | `packages/file-parsers` | **Implemented**. Node manifests are the primary format; the board parsers are retained from VCM's origin and still work |
| 6 | Observed inventory: Redfish captures, `nvidia-smi` CSV/XML, manifest drafting | `packages/file-parsers` (`vcm_parsers.inventory`) | **Implemented** against saved captures and a synthetic sample. Live Redfish capture exists but has **not been run against a real BMC** here |
| 7 | Semantic diff engine, and drift (manifest vs machine) | `packages/diff-engine` | **Implemented** |
| 8 | Engineering dependency graph | `services/engineering-graph` | **Implemented**, with two interchangeable backends: local SQLite and **Appwrite TablesDB** for a shared fleet-wide store |
| 9 | Impact analysis | `services/ai-engine` | **Implemented** as a deterministic rule engine over a component catalog. Node rules: power budget under redundancy policy, chassis cooling, PCIe generation and lanes, driver/CUDA/BIOS/BMC minimums, power connector, DDR generation, DIMM mixing and channel population, socket fit and mixed CPUs, mixed accelerators on one fabric, NIC vs fabric speed, slot count, and measured draw from attached observations. LLM provider interface exists with a no-op default; no real LLM provider ships |
| 10 | Rule plugins for site-specific checks | `services/ai-engine` (`vcm_ai.plugins`) | **Implemented**. Plugins cannot emit `verified` findings; a crashing plugin becomes a finding |
| 11 | Component catalog | `services/ai-engine` | **Implemented**: 90 parts across layers, user and project overrides, JSON/CSV import, per-field source attribution, review worksheets ranked by what your units use, and a trust figure on every report. **No bundled part is reviewed** |
| 11b | Semantic part search | `services/ai-engine` (`vcm_ai.semantic`) | **Implemented** on **Appwrite VectorsDB**: the catalog is embedded and searched by meaning, then every candidate is checked against catalog facts and labelled `drop_in`/`check`/`incompatible`. Tested offline against a fake embedding and vector service; **not run against a live VectorsDB** |
| 12 | Validation | `services/validation` | **Implemented**: built-in checks plus an optional `kicad-cli` wrapper for the board domain (skipped when absent) |
| 13 | Release manifests, verification, rollback planner | `packages/workflow` | **Implemented** (JSON/YAML manifest, re-hash verification, signatures, policy record) |
| 14 | Policies as code (`vcm-policy.json`) gating checks, reviews, releases and verification | `packages/workflow` (`vcm_workflow.policy`) | **Implemented** |
| 15 | Physical traceability: unit registry, signed passports, QR | `services/traceability` | **Implemented**. The unit registry is **Appwrite TablesDB** when configured, so one fleet is shared by every machine; local JSON is the offline cache. Pure-Python QR encoder |
| 16 | Fleet: racks, placement, queries, recalls, rack power, rollout waves, NetBox import | `services/traceability` (`vcm_trace.fleet`, `vcm_trace.fleet_store`, `vcm_trace.netbox`) | **Implemented**. Racks and placement share the fleet store; writes fail closed, reads fall back to the cache and say so. NetBox import tested against saved API JSON, **not against a live NetBox** |
| 17 | Change review records | `packages/workflow` | **Partial**: diff, impact, validation, comments, approvals, policy-aware readiness, identity via API tokens. No merge action |
| 18 | Time machine (`timeline`, `explain`) | `packages/workflow` | **Implemented** |
| 19 | CLI, REST API, dashboard | `packages/vcm-cli`, `services/api` | **Implemented**. The API requires a token (roles: viewer, editor, approver, admin) and refuses to start without one unless `--insecure` |
| 20 | Agent access: MCP server (stdio, offline) with read-only tools and a propose-then-human-approves write path | `services/mcp-server` (`vcm_mcp`) | **Implemented**. Tested over a real stdio subprocess; **not yet exercised with a live agent client**. The dashboard's **Agents** page lists proposals and lets a signed-in approver approve or reject them (the API refuses agents, the proposer and unauthenticated callers) |
| 21 | Agent honesty: claim checker (`vcm_check_claims`) and evals (`vcm eval`) | `vcm_ai.claims`, `vcm_mcp.evals` | **Implemented**. Evals have been run only against reference answerers (one honest, five deliberately flawed), **never against a live model** |
| 22 | Integrations and apps: KiCad plugin, FreeCAD, Git bridge, web app, desktop app | `integrations/`, `apps/` | **Planned**: README stubs only |

## Quickstart

Requires Python 3.11+. The core has no third-party dependencies; the API needs FastAPI, and the
shared fleet, semantic part search and shared graph need the Appwrite SDK (`pip install -e
'.[appwrite]'`). Without Appwrite credentials everything runs against the checkout.

```bash
python3 -m venv .venv && . .venv/bin/activate
make install          # pip install -e ".[dev]"
make test             # pytest
make demo             # end-to-end demo on the sample GPU node
```

On your own node:

```bash
vcm init .
vcm commit -m "current config" --tag v1.0     # snapshot every file, categorised by type
# ...edit the node manifest...
vcm commit -m "proposed upgrade" --tag v1.1
vcm diff v1.0 v1.1                            # semantic diff
vcm impact v1.0 v1.1                          # impact report (--json for JSON)
vcm catalog coverage --rev v1.1               # what the catalog can actually reason about
vcm validate --rev v1.1 --compare v1.0
vcm release create R1 --rev v1.1              # pin hashes
vcm release verify R1
vcm device register NODE-0001 --release R1 --batch RACK-12
vcm device affected --component GPU0 --value "NVIDIA A100 PCIe 40GB"
vcm rollback plan v1.0 --from v1.1
vcm timeline ; vcm explain v1.0 v1.1
```

### Check the declaration against the machine

```bash
vcm inventory fetch-redfish https://10.0.0.21 --user ro --out bmc.json   # explicit network call
nvidia-smi --query-gpu=index,name,serial,pci.bus_id,driver_version,power.draw,power.limit,\
pcie.link.gen.max,pcie.link.width.max --format=csv > gpus.csv
vcm drift --rev v1.0 --redfish bmc.json --nvidia-smi gpus.csv --attach
vcm inventory manifest --redfish bmc.json --out draft.node.json        # bootstrap a manifest
```

Before trusting a new capture, ask it what it left out:

```bash
vcm inventory doctor --redfish bmc.json --nvidia-smi gpus.csv
```

The doctor lists Redfish resources that could not be fetched, kinds that were present but not read,
component types the capture cannot vouch for (drift will not judge them), and parts too thinly
described to match. A parser that is quiet about what it skipped makes a gap look like a clean
result; this is how you find out. To share a capture, or add it to the test suite, scrub it first:

```bash
vcm inventory sanitize --redfish bmc.json --nvidia-smi gpus.csv --out tests/captures/<vendor>-<model>
```

That replaces serials, hostnames, addresses, UUIDs and secrets with stable same-shape stand-ins
(identifiers correlate across files, model names, part numbers and firmware versions are left
alone, and the scrubbed capture parses exactly as the original did), seals the folder with
checksums, and drafts an `expect.json`. The folder then becomes a regression test automatically;
see [`tests/captures/README.md`](tests/captures/README.md). The scrubber cannot know what a site
considers sensitive in free text: read the output before you share it.

`vcm drift` compares only the component types a source can see: a capture without NICs is never
read as "NICs missing". With `--attach`, the observation becomes evidence on the revision, and the
next impact report says whether the base was true, sets measured draw beside the declared estimate,
and projects the new configuration's draw (labelled `speculative`).

### Sign, audit, authenticate

```bash
vcm key gen release-eng --trust               # private key in ~/.vcm/keys, public in trustdb.json
vcm release sign R1 --key release-eng
vcm release verify R1 --require-signature
vcm device sign NODE-0001 --key release-eng
vcm audit verify                              # every commit, release, signature, lock: hash-chained
vcm token create alice --role approver        # API tokens are stored hashed, shown once
vcm serve                                     # binds 127.0.0.1; refuses to start without a token
```

### Fleet

```bash
vcm fleet rack define RACK-12 --budget 17000
vcm fleet place NODE-0001 --rack RACK-12 --position 20      # or: vcm fleet netbox --devices devices.json
vcm fleet query --type gpu --firmware "gpu_driver<535"       # --csv for spreadsheets
vcm fleet recall --mpn M393A4K40DB3                           # grouped by rack and release
vcm fleet power --upgrade-to v1.1 --release R1                # exits 1 if a rack goes over budget
vcm fleet rollout --to v1.1 --release R1 --max-per-rack 1     # waves; blocks critical, defers over-budget
```

### Team sync and locks

```bash
vcm remote add origin /mnt/shared/vcm/gpu-node
vcm push ; vcm pull                            # fast-forward only: VCM never merges hardware
vcm lock acquire gpu-node-01.node.json --reason "re-pinning PSUs" --ttl 4
vcm lock release gpu-node-01.node.json
```

Divergent histories are reported with the way out (compare both sides, keep one, re-apply the
other), never merged. Tags and records that differ between clones are listed as conflicts and left
alone. Commits and pushes that touch a file someone else has locked are refused.

### Let an AI agent use VCM (MCP)

`vcm mcp` serves VCM over the Model Context Protocol on stdio. It runs offline, needs no API key,
and opens no sockets. Point any MCP client at it, for example in `.mcp.json`:

```json
{"mcpServers": {"vcm": {"command": "vcm", "args": ["-C", "/path/to/repo", "mcp"],
                        "env": {"VCM_AGENT_NAME": "claude"}}}}
```

The agent gets 23 read-only tools (diff, impact with confidence labels and evidence, drift against
inline captures, a capture doctor, catalog lookup and review priority, a claim checker, fleet query and recall, rack power, rollout plan, policy check,
release and audit verification, passports, rollback) and two prompts (`review_change`,
`fleet_upgrade_plan`). Its server instructions tell it to report each finding's confidence class and
never upgrade it.

It changes nothing itself. The single write tool, `vcm_propose`, records a **pending proposal** for
one of five actions (create a release, open a review, place a unit, define a rack, register a unit).
A person decides:

```bash
vcm proposal list --status pending
vcm proposal show P0001
vcm proposal approve P0001 --by dana     # performs it as dana, after re-checking it still holds
vcm proposal reject P0001 --by dana --reason "not this quarter"
```

An agent cannot approve, and the person must differ from the proposer. Signing, catalog reviews
(they record that a human checked a datasheet), policy overrides, commits, remotes and locks are
**not proposable**: they stay human. Proposals are validated when made and again when approved; a
release that policy would block cannot even be proposed. The audit log records the agent as
proposer and the person as approver. The API exposes the same decision, needing an `approver`
token.

### Keeping agents honest

An agent can misquote a report: cite a finding that does not exist, promote a `rule_based` finding
to `verified`, round 4190 W into 4000 W, or omit the one critical finding that mattered.
`vcm_check_claims` catches that mechanically before the answer goes out. The agent submits what it
is about to say (finding ids, the confidence class and severity it will state, the numbers it will
quote); VCM recomputes the report itself and returns every mismatch plus every critical finding
that went unmentioned. The `review_change` prompt tells the agent to call it and not to answer
until `honest` is true. It cannot catch bad *reasoning* about correct facts.

`vcm eval` grades an agent on five tricky cases: the headline upgrade, a part the catalog has never
heard of, a base that does not match the machine (and a projection that is only a prediction), a
change that touches nothing (do not invent problems), and instructions planted in a commit message
(data, not orders). Grading is mechanical: claims checked as above, no invented figures or finding
ids, never "safe", and case-specific honesty.

```bash
vcm eval run                         # the honest reference answerer: must pass 5/5
vcm eval run --agent sloppy          # flawed on purpose: must fail (CI checks both)
vcm eval run --agent "python my_agent.py"   # yours; see vcm_mcp.evals.external_agent
```

An external agent gets one JSON object on stdin (`question`, `repo`, `a`, `b`, `mcp_command`) and
prints `{"text": ..., "claims": [...]}`. **No live model has been run against these cases yet.**
Until one has, a pass means only that the grader and the reference agree.

### Policies and site rules

```json
{"schema": "vcm-policy/1", "rules": [
  {"id": "no-critical", "forbid_severity": "critical"},
  {"id": "known-parts", "forbid_unknown_parts": true},
  {"id": "power-needs-two", "require_approvals": 2, "when": {"title_matches": "power|supply"}},
  {"id": "signed", "require_signed_release": true, "stages": ["verify"]}]}
```

Commit this as `vcm-policy.json`. `vcm policy v1.0 v1.1` checks a change; `vcm release create`
refuses a violating release unless `--override-policy REASON` is given, and the override is written
into the manifest and the audit log. Site-specific rules are Python plugins (see
[`examples/plugins/site_rules.py`](examples/plugins/site_rules.py)), enabled with
`VCM_RULE_PLUGINS=module:function` or the `vcm.rules` entry-point group.

### Node manifests

A node is described by a `*.node.json` file. Components declare what they are and which domains
they share; everything else you put on a component is carried through and available to rules.

```json
{
  "schema": "vcm-node/1",
  "node": "gpu-node-01",
  "chassis": {"model": "Supermicro SYS-420GP-TNR", "cooling_w": 3600},
  "power": {"domains": {"PSU_BANK": {"redundancy": "2+2", "watts_each": 2000,
                                     "overhead_w": 400}}},
  "firmware": {"gpu_driver": "535.129.03", "cuda": "12.2"},
  "components": [
    {"ref": "GPU0", "type": "gpu", "model": "NVIDIA H100 NVL 94GB",
     "mpn": "900-21010-0000-000", "slot": "PCIE_SLOT1", "form_factor": "FHFL dual-slot",
     "tdp_w": 400, "pcie_gen": 5, "pcie_lanes": 16, "power_connector": "12VHPWR (16-pin)",
     "connections": {"power": "PSU_BANK", "pcie": "CPU0_PCIE_ROOT",
                     "interconnect": "NVLINK_FABRIC"}}
  ]
}
```

A power domain behaves exactly as a supply rail does on a board, which is why the same graph, diff
and impact engine serve both. Domains shared by every component (a PSU bank, or ground on a board)
are deliberately **not** followed when computing blast radius — otherwise every change would report
the whole node as affected.

## What lives in Appwrite, and why

VCM stores two very different kinds of thing, and they belong in different places.

**Derived data belongs on your disk.** The object store, snapshots and the dependency graph can all
be rebuilt from the design files in front of you. Nothing is lost by keeping them local, so the
graph's default backend is SQLite.

**Facts about physical hardware do not.** A unit is a machine that was built and shipped. It is
registered by whoever is standing in front of it and asked about later by someone else entirely —
an operator planning a rollout, a technician scanning the QR sticker on a front panel, an engineer
running a recall after a bad batch of accelerators. Those are different people on different
machines reasoning about the same hardware.

So the **unit and rack registry is the fleet of record, and it lives in Appwrite TablesDB.** The
local JSON files are a cache. This is not a swappable backend; it is the only arrangement in which
the registry is correct. With a per-clone registry, two engineers who clone the repository have two
different fleets, and this —

```bash
vcm device affected --component GPU0 --value "NVIDIA A100 PCIe 40GB"
```

— silently answers about whichever subset of the fleet that laptop happens to know about. A recall
query is the single question VCM must never get half-right.

```bash
cp .env.example .env     # endpoint, project, API key
vcm fleet health         # is the fleet of record reachable, and how big is it?
vcm fleet provision      # create the fleet tables, columns and indexes
vcm device register NODE-0001 --release R1
vcm fleet recall --model "A100"      # every unit, registered from any machine
```

Two behaviours differ on purpose, because the failure modes are not symmetric:

- **Writes fail closed.** If the fleet of record is unreachable, registering a unit raises and
  nothing is written locally either. A unit recorded only on the laptop that registered it is
  exactly the bug this fixes, so a loud failure beats a half-written registration.
- **Reads degrade open.** Reads fall back to the local mirror when the service is unreachable, but
  the store remembers that it did, and every command that reports to a human prints
  `fleet: LOCAL CACHE, possibly stale`. Stale answers are useful offline; silently stale ones are
  not.

One consequence is worth stating plainly: the registry is shared, but design history is not. A
clone can meet a unit built from a revision it has never fetched, and VCM reports that unit by
serial rather than dropping it or failing the query:

```
DEMO-B1   rack -   release R1   GPU0=NVIDIA A100 PCIe 40GB, ... (+4)
1 unit(s), 8 component(s); by rack: (unplaced) 1
  UNRESOLVED DEMO-A1: revision 9d9e3545fd is not in this checkout (fetch it before closing the incident)
fleet: shared (Appwrite TablesDB)
```

An incident cannot be closed on a silently partial fleet, which is the whole reason the registry
moved out of the checkout.

Without credentials, everything above keeps the old on-disk behaviour with no flags, which is how
the test suite runs. `VCM_FLEET_LOCAL=1` forces it. Repository-scoped records — releases, pull
requests, agent proposals — stay in the checkout, because they travel with the files they describe.

### Semantic part search on VectorsDB

The catalog matches by part-number prefix, which answers *what is this part* and nothing else. When
a part goes end of life the question is the other one — *what could I put here instead* — and
"a dual-slot 300 W accelerator I can run on an existing 8-pin harness" names no part at all.

So the catalog is embedded into **Appwrite VectorsDB** and searched by meaning:

```bash
vcm catalog provision                       # vector collection, dimension probed from the model
vcm catalog index                           # embed all 90 parts
vcm catalog similar "dual-slot 300 W accelerator on an 8-pin harness"
vcm catalog replace "NVIDIA A100 PCIe 40GB"
```

A nearest-neighbour hit is a *plausible* candidate, and plausible is not the standard the rest of
VCM reports at. So `replace` runs every candidate back through the deterministic catalog facts —
type, socket, connector, slot width, PCIe generation, driver floor — and labels it:

```
Replacements for NVIDIA A100 PCIe 40GB (gpu):
  CHECK         NVIDIA H100 NVL  (unreviewed)
                needs a 12VHPWR connector, installed part uses 8-pin EPS
                draws 400 W against 250 W
                requires driver 525.60.13 (installed part needs only 450.80.02)
  INCOMPATIBLE  NVIDIA H100 SXM5  (unreviewed)
                different socket (PCIe vs SXM5)
```

Semantic search proposes; the rule data disposes. The same discipline as the rest of the engine:
nothing is reported as more certain than it is.

### The shared dependency graph

The graph has an Appwrite backend too, for teams that want impact analysis queryable fleet-wide
rather than rebuilt per machine. Unlike the registry this one is genuinely optional — the data is
derived, so SQLite loses nothing but shareability.

```bash
vcm graph provision
vcm graph push --rev v1.1
vcm graph query --component GPU0 --rev v1.1 --backend appwrite
```

Row ids are deterministic hashes, so a re-push upserts rather than duplicating; writes are batched
into bulk upserts; and a revision's subgraph is paged into memory once for traversal, or queried
live with `prefetch=False`. The test suite asserts that an Appwrite-backed graph returns
**byte-identical** results to the local SQLite one.

> **What has actually been run.** Every Appwrite path is exercised against a faithful in-process
> fake (`tests/fake_appwrite.py`) under both SDK response shapes, because the suite must pass
> without network. Beyond that: the **fleet store has been run against a live Appwrite project** —
> provisioned, and verified with two separate clones registering units into one fleet, including
> the cross-clone duplicate rejection and the stale-cache path. The **VectorsDB path has not been
> run live**: it needs the `embeddings.write` and `vectors.*` scopes on the API key, and the
> commands say so when they are missing.

## Confidence classes

Every finding has exactly one `confidence_class`, plus evidence links (`file` with hash, `ref`,
`commit`, `db` with per-field attribution, `declared`, `evidence`, `plugin`). The report's shape is
published as [`docs/schemas/impact-report.schema.json`](docs/schemas/impact-report.schema.json)
(`vcm-impact/1`):

- `verified` — derived from parsed configuration data, declared project links or stored evidence.
- `rule_based` — a heuristic from engineering rules and the component catalog.
- `speculative` — a prediction; not established by data.

An optional LLM provider can only ever add `speculative` findings, and a site rule plugin only
`rule_based` or `speculative` ones; neither can state a fact. The default provider is a no-op
and VCM never calls the network by default.

**Silence is never safety.** If a changed component is not in the catalog, VCM says so explicitly
rather than returning a clean-looking report:

> **8 components: NVIDIA B200 SXM 192GB is not in the component catalog** — Affected: GPU0 … GPU7
> (part 900-74000-0000-000). The catalog has no entry, so VCM skipped driver-minimum, PCIe
> generation and power-connector checks, all of which read the catalog. Rules fed by values
> declared in the node manifest (power budget, chassis cooling, PCIe lane counts) did still run for
> it. Absence of warnings from those checks is absence of knowledge, not evidence of a safe change.

Note what that message does *not* do: it does not claim more ignorance than there is. Rules driven
by manifest-declared values keep working on an unknown part, and the disclosure says so rather than
flatly announcing that nothing ran.

Every impact report also carries a coverage summary (`Catalog covered 8 of 8 changed components`),
so a reader can always tell how much of the configuration the rules actually reasoned about.

## The component catalog

90 parts in three bundled layers, overridable per user (`~/.vcm/catalog/`) and per project
(`vcm-catalog/`, committed next to the configuration). Later layers win; lookup is longest-prefix,
so a specific part number beats a family entry.

```bash
vcm catalog list --type gpu
vcm catalog show "NVIDIA H100 NVL 94GB"
vcm catalog import our-parts.csv --scope project
```

**No bundled entry has been datasheet-reviewed.** Every part carries `reviewed: false` and a named
`source`; thermal resistance figures are package-typical estimates, not part data; TDP is a board
power rating, not measured draw under your workload.

Rule findings cite each value they used, field by field: whether it came from the node manifest or
the catalog, the catalog's source for that field, and who verified it. Record a review with

```bash
vcm catalog review 900-21010-0000-000 --by alice --source "H100 NVL datasheet DS-11717 p.3" \
    --confirm tdp_w --confirm min_driver --set pcie_gen=5
vcm catalog unreviewed --type gpu
```

A part becomes `reviewed: true` only once every rule-relevant field it carries has been verified by
a named person against a cited source.

To spend reviewing time where it buys the most trust:

```bash
vcm catalog priority                       # parts your registered units use most, and how much of
                                           #   each has been checked (0 of 31 values, today)
vcm catalog worksheet --top 15 --out review.csv
# a person opens review.csv: for each value they check against the datasheet, set decision to ok
# (or correct, with corrected_value), cite the document and page in datasheet_source, and put
# their own name in checked_by. Rows left blank are not touched.
vcm catalog review --from-csv review.csv   # every row is validated first; one bad row, nothing applied
```

The importer refuses a row with no named person, one naming an agent, one with no source, a
`correct` with no value, and an `ok` for a value that changed since the sheet was written (that
would verify a number nobody looked at). Every impact report then carries a trust figure
(`summary.trust`): how many catalog values its findings cite as evidence and how many a named person
verified. It is a floor, not a census: a rule may use inputs it does not cite individually.

## Limitations

- **The catalog is small and unreviewed.** 90 parts, none datasheet-verified by a human. Rules are
  heuristics and can be wrong or incomplete. VCM discloses what it does not know; it cannot
  disclose what it has wrong.
- Observed-inventory parsing is tested against a **synthetic** Redfish capture (and `tests/captures/`
  is where real, scrubbed ones go; none has been contributed yet) and `nvidia-smi`
  output written for the demo. BMC implementations vary; the live Redfish fetcher and the NetBox
  fetcher have not been run against real services here.
- Power and thermal rules use declared and catalog figures. Attached telemetry is a point-in-time
  reading under whatever workload was running, and projecting it onto a new configuration is a
  prediction, labelled as such.
- The board domain's schematic connectivity is not traced; nets come from PCB pad assignments. The
  sample KiCad files are hand-generated and have not been opened in KiCad.
- Remotes are a shared directory: no network transport, no server-side access control beyond the
  file system's. History is fast-forward only, by design; there is no merge.
- The shared fleet makes the **registry** fleet-wide, not the design history. A clone that has not
  fetched a unit's revision cannot describe that unit, and says so (`UNRESOLVED`) rather than
  omitting it. Pair it with `vcm remote` so engineers share history too.
- Row-level Appwrite permissions are **not** used yet: VCM talks to TablesDB with a server API key,
  which bypasses them. Access control is at the VCM API's token roles, so anyone holding the
  Appwrite key has full access to the fleet tables. Protect the key accordingly.
- Semantic part search depends on Appwrite's embedding models, and its shortlist is only as good as
  the catalog text behind it. Every candidate is checked against catalog facts before it is shown,
  but an unreviewed catalog entry can make a confident-looking verdict wrong.
- Signatures prove which trusted key vouched for a release or passport. The trust database is a
  committed file, so whoever can change and commit it decides whom to trust; protect it like code.
  The Ed25519 implementation is pure Python and **not constant-time**: fine for signing manifests
  offline, not for use where an attacker can time it.
- The audit chain detects edits and deletions, but someone with write access to `.vcm/` can rewrite
  the whole chain; signed releases anchor its head, which is what makes a rewrite detectable.
- API identities come from tokens; there is no SSO, password login or per-object permission. Use
  HTTPS in front of it if it leaves localhost.
- Evals and the claim checker grade honesty and fidelity to the report, not insight, and the evals
  have only been exercised with scripted answerers. A model that passes can still reason badly
  about correct facts; one that fails shows where to look.
- The MCP server is tested against the protocol (including a real stdio subprocess) but has not
  been driven by a live agent client here. An agent's reasoning is only as good as VCM's data: it
  inherits every limitation above, and an agent can still misread a result, which is why each
  finding carries its confidence class and evidence. Proposals are approved from the CLI, the API or the dashboard's
  Agents page; the dashboard needs an API token with the approver role to decide (insecure mode
  cannot).
- Integrations (KiCad, FreeCAD, Git bridge), the web app and the desktop app are not implemented.

## Repository map

```
packages/core            object store, snapshots, audit log, signing, remotes, tokens  (vcm_core)
packages/file-parsers    node manifests, observed inventory, KiCad, BOM               (vcm_parsers)
packages/diff-engine     semantic hardware diff, drift                                (vcm_diff)
packages/workflow        releases, rollback, reviews, policies, timeline              (vcm_workflow)
packages/vcm-cli         `vcm` command                                                (vcm_cli)
services/engineering-graph  dependency graph: SQLite + Appwrite                       (vcm_graph)
services/ai-engine       impact rules, rule plugins, component catalog                (vcm_ai)
services/validation      checks and evidence                                          (vcm_validation)
services/traceability    units, passports, QR, fleet, NetBox                          (vcm_trace)
services/api             FastAPI app + dashboard                                      (vcm_api)
services/mcp-server      MCP server for agents, proposals                             (vcm_mcp)
examples/gpu-node        sample node, v1.0 and v1.1, plus synthetic machine captures
examples/plugins         example site rules
examples/demo.sh         end-to-end node demo
examples/demo-pcb.sh     end-to-end board demo (the original domain)
docs/                    architecture, data model, roadmap, report schema
```

Licensed under Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md).
