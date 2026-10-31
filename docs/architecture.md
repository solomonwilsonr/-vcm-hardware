# Architecture

VCM is a set of small Python packages that share one on-disk repository format (`.vcm/`). Dependencies point one way:

```
core -> file-parsers -> diff-engine -> engineering-graph -> validation -> ai-engine
                                                   traceability (devices, QR)
                                                   workflow (release, rollback, PR, timeline)
                                                   vcm-cli, api
```

| Package | Import name | Responsibility |
|---|---|---|
| `packages/core` | `vcm_core` | SHA-256 object store, revision (snapshot) model, refs, working-tree scan, commit/checkout, evidence and record storage |
| `packages/file-parsers` | `vcm_parsers` | S-expression parser, KiCad schematic/PCB extraction, BOM CSV, merge into a `Design` |
| `packages/diff-engine` | `vcm_diff` | Semantic diff of two `Design`s or two revisions, JSON and text output |
| `services/engineering-graph` | `vcm_graph` | Builds the dependency graph of a revision into SQLite and answers "what is affected by X" |
| `services/validation` | `vcm_validation` | Built-in checks, optional `kicad-cli` wrapper, versioned evidence, cross-revision comparison |
| `services/ai-engine` | `vcm_ai` | Deterministic impact analysis, LLM provider interface, Markdown rendering |
| `services/traceability` | `vcm_trace` | Device registry, passports, affected-device queries, QR encoder |
| `packages/workflow` | `vcm_workflow` | Release manifests and verification, rollback planner, pull-request records, timeline and explain |
| `packages/vcm-cli` | `vcm_cli` | `vcm` command (argparse) |
| `services/api` | `vcm_api` | FastAPI REST API and static dashboard |

## Storage

Everything lives under `<project>/.vcm/`: content-addressed `objects/`, `refs/heads`, `refs/tags`, `HEAD`, `evidence/<revision>/<kind>.json`, `releases/`, `devices/`, `prs/` and the SQLite `graph.db`. See [data-model.md](data-model.md).

Blobs are written once (dedup by SHA-256), streamed in 1 MiB chunks so large binaries (STEP, Gerber zips, bitstreams) do not need to fit in memory, and made read-only. A revision is itself an object: its id is the hash of its canonical JSON, so history is tamper-evident against accidental change.

## Data flow for a question

1. `vcm impact A B` resolves both revisions and parses their files into `Design` models (components, pad-to-net maps, placement).
2. `diff_designs` yields the structured diff and the set of changed components.
3. `build_graph` writes nodes and edges for revision B into `graph.db`; `affected_by(ref)` walks `connects_to` edges through non-ground nets and follows `configured_by`, `validated_by` and `placed_on`.
4. Rules in `vcm_ai.engine` produce findings in three classes and a re-test list (see below).
5. The report is returned as JSON and can be rendered as Markdown with the classes in separate sections.

## Confidence classes

| Class | Meaning | Examples in the engine |
|---|---|---|
| `verified` | Derived from parsed data, declared project links (`vcm.json`) or stored validation evidence | replaced part, footprint changed, nets shared, firmware linked to an affected component, validation delta |
| `rule_based` | Heuristic from the component DB and engineering rules | current headroom, dropout, junction temperature estimate, ESR-sensitive LDO with ceramic cap, pinout consistency, capacitor derating, firmware supply settings |
| `speculative` | Prediction that is not established | brownout during radio bursts, enclosure heating, ripple change; any LLM output |

Each finding carries `evidence` entries of type `file` (path, hash, revision), `ref` (component or net) or `commit`, and optionally `derived_from` (ids of the findings that triggered a speculative one).

## Extension points

- **LLM provider**: implement `vcm_ai.LLMProvider` (`name`, `complete(prompt) -> str | None`) and `register_provider(name, factory)`. It is only used when selected (`vcm impact --llm NAME`), receives a summary of deterministic findings, and its output is always labelled `speculative`. The default `none` provider performs no I/O.
- **Parsers**: add a function returning `Component`s and merge it in `vcm_parsers/design.py`.
- **Component DB**: extend `services/ai-engine/vcm_ai/data/components.json` (`match` prefixes, `type`, ratings, `pinout`).
- **Graph backend**: the graph is two tables (`nodes`, `edges`) keyed by revision. A PostgreSQL implementation of `EngineeringGraph` with the same methods is the intended production direction; it is not built.

## Security and trust notes

- Hash checks detect modification of stored blobs and manifests, and releases can carry Ed25519 signatures checked against the committed `trustdb.json`. Signing is opt-in: an unsigned release is only protected by its hashes, so an attacker with write access to `.vcm/` could rewrite and re-hash it, and could strip signatures unless `verify --require-signed` is used.
- The API has no authentication or authorisation. Run it on localhost or behind your own access control.
- Paths from the working tree are stored as POSIX relative paths; release and device names are validated against a conservative pattern before being used as file names.
- Parsers treat file content as untrusted data (no evaluation). The `kicad-cli` wrapper passes file paths as argv entries, never through a shell.
