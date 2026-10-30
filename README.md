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

> **Status: working MVP, honest scope.** Everything marked *Implemented* below runs and is covered
> by tests in this repository. Everything marked *Planned* does not exist yet. See
> [Limitations](#limitations).

## The demo in one paragraph

A node runs eight A100 PCIe accelerators on four 2000 W supplies in a 2+2 redundant bank. Someone
proposes swapping them for H100 NVL. The manifest diff is eight replaced line items. What VCM says
is that the node now draws **4190 W against a 4000 W usable budget**, so losing one supply drops it;
that the **installed driver (470.223.02) is below the H100's minimum (525.60.13)**, so the cards
will not initialise at all; that the chassis is rated for 3600 W of cooling; that the Gen5 cards sit
on a Gen4 host root; that both PCIe roots are lane-oversubscribed; and that the power connector
changed from 8-pin EPS to 12VHPWR, so the existing harness does not fit.

None of that is written down anywhere. It falls out of the graph plus a component catalog.

```
$ vcm impact v1.0 v1.1
Findings: verified 6, rule_based 8, speculative 3 | severity: critical 2, warning 9, info 12
```

## Modules: implemented vs planned

| # | Module | Location | Status |
|---|--------|----------|--------|
| 1 | Content-addressed object store, snapshots, branches, tags, commit/log/checkout/diff, evidence store | `packages/core` | **Implemented** (local, single-user; no merge, no remotes) |
| 2 | Input parsers: node manifests (`*.node.json`), KiCad `.kicad_sch`/`.kicad_pcb`, BOM CSV | `packages/file-parsers` | **Implemented**. Node manifests are the primary format; the board parsers are retained from VCM's origin and still work |
| 3 | Semantic diff engine | `packages/diff-engine` | **Implemented** for inventory, form factor/slot, placement and connectivity |
| 4 | Engineering dependency graph | `services/engineering-graph` | **Implemented**, with two interchangeable backends: local SQLite and **Appwrite TablesDB** for a shared fleet-wide store |
| 5 | Impact analysis | `services/ai-engine` | **Implemented** as a deterministic rule engine over a component catalog. Node rules: power budget under redundancy policy, chassis cooling, PCIe generation and lane budget, driver minimums, power-connector change. LLM provider interface exists with a no-op default; no real LLM provider ships |
| 6 | Component catalog | `services/ai-engine` | **Implemented**: 86 parts across three layers, user and project overrides, JSON/CSV import, and explicit coverage reporting |
| 7 | Validation | `services/validation` | **Implemented**: built-in checks plus an optional `kicad-cli` wrapper for the board domain (skipped when absent) |
| 8 | Release manifests, verification, rollback planner | `packages/workflow` | **Implemented** (JSON/YAML manifest, tamper detection by re-hashing, domain-aware rollback wording) |
| 9 | Physical traceability: unit registry, passports, QR | `services/traceability` | **Implemented** with a local JSON registry and a pure-Python QR encoder |
| 10 | Change review records | `packages/workflow` | **Partial**: local review record (diff, impact, validation, comments, approvals). No merge action, identity or permissions |
| 11 | Time machine (`timeline`, `explain`) | `packages/workflow` | **Implemented** |
| 12 | CLI, REST API, dashboard | `packages/vcm-cli`, `services/api` | **Implemented** (no authentication; intended for local/trusted use) |
| 13 | Integrations and apps: BMC/Redfish import, `nvidia-smi` ingest, web app, desktop app | `integrations/`, `apps/` | **Planned**: README stubs only |

## Quickstart

Requires Python 3.11+. The core has no third-party dependencies; the API needs FastAPI, the shared
graph backend needs the Appwrite SDK.

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

## Shared fleet graph on Appwrite TablesDB

A single engineer's history belongs on their disk. A **fleet** does not: "which of our nodes are
affected by this driver change?" is a question about every node, so the graph has a second backend
that puts the same two tables in Appwrite TablesDB.

```bash
cp .env.example .env     # endpoint, project, API key
vcm graph health         # check the connection
vcm graph provision      # create the database, tables, columns and indexes
vcm graph push --rev v1.1
vcm graph query --component GPU0 --rev v1.1 --backend appwrite
```

Design files stay local; only the graph is shared. Row ids are deterministic hashes, so a re-push
upserts rather than duplicating; writes are batched into bulk upserts; and a revision's subgraph is
paged into memory once for traversal, or queried live with `prefetch=False`. The test suite asserts
that an Appwrite-backed graph returns **byte-identical** results to the local SQLite one.

## Confidence classes

Every finding has exactly one `confidence_class`, plus evidence links (`file` with hash, `ref`,
`commit`, `db`, `declared`):

- `verified` — derived from parsed configuration data, declared project links or stored evidence.
- `rule_based` — a heuristic from engineering rules and the component catalog.
- `speculative` — a prediction; not established by data.

An optional LLM provider can only ever add `speculative` findings. The default provider is a no-op
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

86 parts in three bundled layers, overridable per user (`~/.vcm/catalog/`) and per project
(`vcm-catalog/`, committed next to the configuration). Later layers win; lookup is longest-prefix,
so a specific part number beats a family entry.

```bash
vcm catalog list --type gpu
vcm catalog show "NVIDIA H100 NVL 94GB"
vcm catalog import our-parts.csv --scope project
```

**No bundled entry has been datasheet-reviewed.** Every part carries `reviewed: false` and a named
`source`; thermal resistance figures are package-typical estimates, not part data; TDP is a board
power rating, not measured draw under your workload. Rule findings cite that provenance in their
evidence, and the coverage finding states it in words. Mark an entry `reviewed: true` only when a
human has actually checked it.

## Limitations

- **The catalog is small and unreviewed.** 86 parts, none datasheet-verified by a human. Rules are
  heuristics and can be wrong or incomplete. VCM discloses what it does not know; it cannot
  disclose what it has wrong.
- Node manifests are hand-authored. There is no BMC/Redfish, `nvidia-smi`, `lspci` or `dmidecode`
  import yet, so the manifest can drift from the machine it describes.
- Power and thermal rules use declared and catalog figures, not telemetry. Real draw under a real
  workload is not modelled.
- The board domain's schematic connectivity is not traced; nets come from PCB pad assignments. The
  sample KiCad files are hand-generated and have not been opened in KiCad.
- Single-user local repositories: no remotes, merge, locking, authentication or permissions outside
  the shared graph backend. Revision history is first-parent only.
- Release verification detects accidental or naive tampering by re-hashing; there are no
  signatures, so someone who can rewrite both manifest and store can forge a release.
- The REST API has no authentication and is meant for localhost or trusted networks.
- Integrations, the web app and the desktop app are not implemented.

## Repository map

```
packages/core            object store, snapshots, repository      (vcm_core)
packages/file-parsers    node manifests, KiCad, BOM                (vcm_parsers)
packages/diff-engine     semantic hardware diff                    (vcm_diff)
packages/workflow        releases, rollback, reviews, timeline     (vcm_workflow)
packages/vcm-cli         `vcm` command                             (vcm_cli)
services/engineering-graph  dependency graph: SQLite + Appwrite    (vcm_graph)
services/ai-engine       impact rules + component catalog          (vcm_ai)
services/validation      checks and evidence                       (vcm_validation)
services/traceability    units, passports, QR                      (vcm_trace)
services/api             FastAPI app + dashboard                   (vcm_api)
examples/gpu-node        sample node, v1.0 and v1.1
examples/demo.sh         end-to-end node demo
examples/demo-pcb.sh     end-to-end board demo (the original domain)
docs/                    architecture, data model, roadmap
```

Licensed under Apache-2.0. See [CONTRIBUTING.md](CONTRIBUTING.md).
