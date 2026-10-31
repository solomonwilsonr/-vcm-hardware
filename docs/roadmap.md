# Roadmap (proposed, not guaranteed)

This is a proposed 12-week plan in four phases. Dates, scope and order are planning assumptions and may change. Nothing here is a commitment.

## Where we are

Done since the MVP: release and passport signing, a hash-chained audit log, API authentication with
roles, Redfish and `nvidia-smi` import with drift detection and measured draw, more node rules with
per-field catalog attribution and a review workflow, rule plugins, fleet queries, recalls, rack
power and rollout waves, NetBox placement import, directory remotes with locks, policies as code,
and a published impact-report schema. See the README table for exact scope.

The phases below are the original proposal, kept for reference. Items already delivered are struck
through only in spirit: read the README table, not this list, for what exists.

Next, in order of credibility gained:

1. Real captures from real BMCs and a real NetBox, replacing the synthetic samples, and fixes for
   whatever they reveal.
2. Human datasheet reviews of the catalog entries the demo relies on.
3. Real KiCad fixtures (de-risks schematic net tracing).
4. A network transport for remotes (S3 or Git-backed).

## Phase 1 (weeks 1-3): harden the engineering core

- Schematic connectivity: trace wires, labels and hierarchical sheets so nets exist without a PCB.
- Broader KiCad coverage (zones, tracks for routed-length and connectivity diff, differential pairs, variants and DNP).
- Test against real KiCad 8/9 projects and run `kicad-cli` DRC/ERC in CI with KiCad installed.
- Merge-free branching workflow polish: three-way compare, `vcm status` against releases.

## Phase 2 (weeks 4-6): knowledge and trust

- Grow the component DB (importable from CSV/JSON, versioned, with source attribution per value).
- More rules: connector pinout, decoupling per power pin, net voltage propagation, DNP handling, firmware pin-map checks.
- Key expiry, passphrase-protected or hardware-backed keys. Ed25519 release signing, passport
  signing and key revocation are done (`vcm release sign`, `vcm device sign`, `vcm key revoke`).
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
