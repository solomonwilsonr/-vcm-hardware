# Architecture

VCM is a set of small Python packages that share one on-disk repository format (`.vcm/`). Dependencies point one way:

```
core -> file-parsers -> diff-engine / engineering-graph / validation -> ai-engine
                                                   traceability (devices, QR, fleet, NetBox)
                                                   workflow (release, rollback, PR, policy, timeline)
                                                   vcm-cli, api
```

| Package | Import name | Responsibility |
|---|---|---|
| `packages/core` | `vcm_core` | SHA-256 object store, revision (snapshot) model, refs, working-tree scan, commit/checkout, evidence and record storage, hash-chained audit log, Ed25519 signing and trust database, API tokens, remotes and locks |
| `packages/file-parsers` | `vcm_parsers` | Node manifests, S-expression parser, KiCad schematic/PCB extraction, BOM CSV, merge into a `Design`; observed inventory from Redfish and `nvidia-smi` captures |
| `packages/diff-engine` | `vcm_diff` | Semantic diff of two `Design`s or two revisions, JSON and text output; drift between a manifest and an observation |
| `services/engineering-graph` | `vcm_graph` | Builds the dependency graph of a revision into SQLite and answers "what is affected by X" |
| `services/validation` | `vcm_validation` | Built-in checks, optional `kicad-cli` wrapper, versioned evidence, cross-revision comparison |
| `services/ai-engine` | `vcm_ai` | Deterministic impact analysis, component catalog, rule plugins, LLM provider interface, Markdown rendering |
| `services/traceability` | `vcm_trace` | Device registry, signed passports, affected-device queries, QR encoder, racks and fleet queries, rack power, rollout planning, NetBox import |
| `packages/workflow` | `vcm_workflow` | Release manifests, signing and verification, policies, rollback planner, pull-request records, timeline and explain |
| `packages/vcm-cli` | `vcm_cli` | `vcm` command (argparse), plus the only two network fetchers (Redfish, NetBox), run only on explicit commands |
| `services/api` | `vcm_api` | FastAPI REST API (token-authenticated, role-checked) and static dashboard |

## Storage

Everything lives under `<project>/.vcm/`: content-addressed `objects/`, `refs/heads`, `refs/tags`, `refs/remotes/`, `HEAD`, `evidence/<revision>/<kind>.json`, `releases/`, `devices/`, `racks/`, `prs/`, `audit/events.jsonl`, `tokens.json` (hashed), `remotes.json` and the SQLite `graph.db`. Committed at the project root: `trustdb.json` (trusted public keys), `vcm-policy.json`, and `vcm-catalog/`. Private signing keys live outside the project in `~/.vcm/keys`. See [data-model.md](data-model.md).

Blobs are written once (dedup by SHA-256), streamed in 1 MiB chunks so large binaries (STEP, Gerber zips, bitstreams) do not need to fit in memory, and made read-only. A revision is itself an object: its id is the hash of its canonical JSON, so history is tamper-evident against accidental change.

## Data flow for a question

1. `vcm impact A B` resolves both revisions and parses their files into `Design` models (components, pad-to-net maps, placement).
2. `diff_designs` yields the structured diff and the set of changed components.
3. `build_graph` writes nodes and edges for revision B into `graph.db`; `affected_by(ref)` walks `connects_to` edges through non-ground nets and follows `configured_by`, `validated_by` and `placed_on`.
4. Rules in `vcm_ai.engine` and `vcm_ai.rules_datacenter` produce findings in three classes and a re-test list (see below). If an observation of the base revision is attached as evidence, drift and measured draw are added. Rule plugins then run.
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
- **Rule plugins**: `vcm_ai.register_rule`, the `vcm.rules` entry-point group, or `$VCM_RULE_PLUGINS`. Plugins receive a `RuleContext` and can add `rule_based` or `speculative` findings only; failures become findings.
- **Component catalog**: project overrides in `vcm-catalog/`, user overrides in `~/.vcm/catalog/`, imported with `vcm catalog import`; reviews recorded with `vcm catalog review`.
- **Policies**: `vcm-policy.json` committed with the design (see `vcm_workflow.policy`).
- **Graph backend**: the graph is two tables (`nodes`, `edges`) keyed by revision. A PostgreSQL implementation of `EngineeringGraph` with the same methods is the intended production direction; it is not built.

## Security and trust notes

- Hash checks detect modification of stored blobs and manifests. Releases and unit records can be signed with Ed25519 keys; verification checks signatures against the committed `trustdb.json`, offline. Signatures are bound to a purpose (`release` or `passport`).
- The audit log chains every state change by hash; release manifests record its head. Someone with write access to `.vcm/` can still rewrite the whole chain, which is why the head is anchored in signed releases.
- The API requires a bearer token and refuses to start without one unless `--insecure` is explicit. Tokens are stored as SHA-256 hashes; roles are viewer, editor, approver and admin. Run it behind HTTPS if it leaves localhost.
- The pure-Python Ed25519 is not constant-time; it is meant for offline signing of manifests, not for an attacker-timed service.
- Paths from the working tree are stored as POSIX relative paths; release, device, rack and key names are validated against a conservative pattern before being used as file names.
- Parsers treat file content as untrusted data (no evaluation). The `kicad-cli` wrapper passes file paths as argv entries, never through a shell. Rule plugins are code the operator installs or names explicitly; nothing is imported from a design repository.
