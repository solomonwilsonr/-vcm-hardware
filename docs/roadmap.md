# Roadmap (proposed, not guaranteed)

This is a proposed 12-week plan in four phases. Dates, scope and order are planning assumptions and may change. Nothing here is a commitment.

## Where we are

The MVP described in the README exists: local repositories, KiCad/BOM parsing, semantic diff, SQLite graph, rule-based impact analysis, validation evidence, releases, rollback planning, device traceability, local pull requests, CLI, REST API and dashboard.

## Phase 1 (weeks 1-3): harden the engineering core

- Schematic connectivity: trace wires, labels and hierarchical sheets so nets exist without a PCB.
- Broader KiCad coverage (zones, tracks for routed-length and connectivity diff, differential pairs, variants and DNP).
- Test against real KiCad 8/9 projects and run `kicad-cli` DRC/ERC in CI with KiCad installed.
- Merge-free branching workflow polish: three-way compare, `vcm status` against releases.

## Phase 2 (weeks 4-6): knowledge and trust

- Grow the component DB (importable from CSV/JSON, versioned, with source attribution per value).
- More rules: connector pinout, decoupling per power pin, net voltage propagation, DNP handling, firmware pin-map checks.
- Signed device passports, key revocation/expiry, passphrase-protected or hardware-backed keys (release signing itself is done: Ed25519, `vcm release sign`).
- Evidence ingestion: attach bench results, lab reports and CI outputs to revisions and link them to graph tests.

## Phase 3 (weeks 7-9): collaboration

- Remote repositories and sync (git-backed transport first), shared object store.
- Pull requests with identities, required reviewers, merge action and branch protection rules.
- PostgreSQL graph backend and a multi-repository service mode for the API with authentication.
- Git bridge: map VCM revisions to Git commits and tags; store large artifacts via the VCM object store.

## Phase 4 (weeks 10-12): reach

- KiCad plugin (diff and impact inside the editor) and a FreeCAD integration for mechanical parts.
- Web app (reviews, graph explorer, device registry) and a desktop shell.
- Optional LLM providers behind the existing interface (local-first), keeping the `speculative` labelling.
- Mechanical and FPGA semantic diff (STEP assemblies, constraint files) as separate parsers.
- GPU Git as a separate, interoperable module only if the GPUOS/VACE requirements are confirmed (see [gpu-git.md](gpu-git.md)).
