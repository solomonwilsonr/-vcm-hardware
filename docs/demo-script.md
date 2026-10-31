# 60-second demo script

One sentence: **Git for datacenter hardware — change one GPU model in a node and VCM tells you what
broke, how sure it is, and which racks are affected.**

If a beat below can't be said in its time slot, cut the beat, not the voice-over.

## The story

A fleet owner wants to upgrade `gpu-node-01` from 8x A100 to 8x H100 NVL. Same chassis, same PSUs.
"Should be a drop-in." It isn't.

## Beats

| Time | Say | Show (command) | What the audience must see |
|------|-----|----------------|----------------------------|
| 0:00–0:08 | "Hardware changes are tracked in spreadsheets. A GPU swap looks like one line, but it touches power, cooling and firmware." | Title slide / `vcm log` with v1.0 and v1.1 | Two committed configurations |
| 0:08–0:20 | "VCM diffs the node inventory, not bytes. One finding, not eight — the 8 GPUs are grouped." | `vcm diff v1.0 v1.1` | One grouped replacement line |
| 0:20–0:38 | "Then it asks what else is affected. Every finding is labelled by how sure we are: verified from data, rule-based from engineering rules, or speculative." | `vcm impact v1.0 v1.1 | sed -n 1,36p` | Verified section, then the R001 power-budget finding under Rule-based, with evidence |
| 0:38–0:50 | "Releases are hash-pinned, and each physical node is registered against one. So the question becomes: which units are affected?" | `vcm device affected --component GPU0 --value "NVIDIA A100 PCIe 40GB"` | Named units and racks |
| 0:50–0:60 | "Corrupt one byte and verification fails. Local-first, no network, Apache-2.0." | `vcm release verify R1.1` (pass), then tamper and re-run (fail) | Red failure on tampered blob |

## Honesty line (say it, don't skip it)

"Catalog data is demo-grade and unreviewed, and there are no signatures or API auth yet. The
mechanism is real; the knowledge and trust layers are next." This matches the README's
implemented-vs-planned table.

## Cut list (in `examples/demo.sh`, not in the 60 s)

`catalog coverage`, `graph query`, `validate`, QR/passport, `rollback plan`, `pr open/approve`,
`timeline`, `explain`. Keep them for Q&A.

## Rehearsal checklist

- `make demo` passes from a clean clone with no network.
- `examples/demo-60s.sh` finishes in under 60 s of *talking*, not just runtime; pause at each step.
- Terminal font 18pt+, one command per beat, output trimmed with `sed -n '1,N p'`.
- Fallback if the live run fails: pre-recorded `examples/demo-60s.sh | tee` output.
