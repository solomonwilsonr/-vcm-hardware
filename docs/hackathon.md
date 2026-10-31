# Appwrite Databases Hackathon — VCM

**VCM: version control for datacenter hardware, with a shared fleet of record on Appwrite TablesDB.**

Every hardware change has a blast radius. VCM tells you what a node change breaks, which physical
units are affected, and how much to trust each statement. Every finding is labelled `verified`,
`rule_based` or `speculative`, with evidence behind it. A guess is never presented as a fact.

Built during the hackathon window (first commit 2026-10-30). Solo.

## Why TablesDB

The question VCM must never answer wrongly is *"which physical units are affected?"* After a bad
batch of accelerators, an operator on one machine asks about units that a technician registered
on another. A per-checkout registry silently answers about a subset of the fleet. A shared
database fixes that, and the data is naturally relational and typed:

| Table | Columns that are indexed | Why |
|---|---|---|
| `vcm_devices` | `name`, `release`, `revision`, `rack` (plus `batch`) | "every unit on release R1" is an index lookup, not a scan |
| `vcm_racks` | `name`, `site` | rack power and rollout waves group by rack |
| `vcm_nodes`, `vcm_edges` | `(rev, node_id)`, `(rev, src, type)`, `(rev, dst, type)` | the dependency graph, one store for every revision |

Details that were chosen on purpose (see `services/traceability/vcm_trace/fleet_store.py`):

- **Writes fail closed, reads degrade open.** If TablesDB is unreachable, registering a unit
  raises. A unit recorded only on one laptop is the bug being fixed. Reads fall back to a local
  cache and say they did.
- **Deterministic row ids**, so a re-push upserts instead of duplicating, and a duplicate
  registration from a second clone is rejected.
- **Bulk upserts** for graph pushes. One `doc` column carries the full record, so adding a field
  never needs a migration.
- **Byte-identical results** between the Appwrite graph backend and the local SQLite one, asserted
  in the test suite.

## What has been run, and what has not

- Run live: the fleet store on a real Appwrite project, provisioned, with two separate clones
  registering units into one fleet (including the cross-clone duplicate rejection).
- Run against an in-process fake only: the graph backend and the VectorsDB part search.
- Not real yet: the catalog has not been reviewed against datasheets, the machine captures are
  synthetic, and the web app, desktop app and FreeCAD pieces are placeholders.
  The README's implemented-vs-planned table is the source of truth.

## Reproduce it

```bash
python3 -m venv .venv && . .venv/bin/activate && make install
make demo                  # offline, end to end (verified from a fresh clone)

cp .env.example .env       # fill in endpoint, project, API key, database
vcm fleet health
vcm fleet provision        # creates the tables and indexes
bash examples/demo-fleet.sh   # two checkouts, one shared fleet
```

## 90-second demo script

1. **(0:00)** "Someone proposes swapping eight A100s for H100 NVL." Run `vcm impact v1.0 v1.1`.
   Point at the count line, then at the critical findings: 4190 W against a 4000 W budget, and a
   driver below the H100 minimum.
2. **(0:25)** Show the confidence labels. "`verified` comes from the data, `rule_based` from
   engineering rules, `speculative` is a guess. VCM will not call a guess a fact."
3. **(0:40)** Run `bash examples/demo-fleet.sh`. Two separate checkouts register units. Open the
   Appwrite console on `vcm_devices` and show the rows. "Each checkout answers for the whole fleet."
4. **(1:00)** `vcm device affected` for the old release: the recall question, answered fleet-wide.
5. **(1:10)** Show the guardrail: an agent can only propose, a named person approves. Then the
   tamper check at the end of `make demo`, which fails as expected.
6. **(1:25)** "Offline by default, signed releases, honest about what is not built yet."

## On your own environment (fastest path)

```bash
cp .env.example .env     # fill in the four VCM_APPWRITE_* values (comments inside explain each)
make hackathon           # checks credentials, provisions, pushes the graph, runs the shared-fleet demo
```

It stops at the first problem and says what to fix. Then screenshot the `vcm_devices`,
`vcm_nodes` and `vcm_edges` tables in the Appwrite console for the video.

## Submission checklist (due Oct 31, 11:59 PM PT)

- [ ] Repository is **public**
- [ ] `make hackathon` ran clean against your Appwrite project
- [ ] Demo video recorded (script above, about 90 seconds) and uploaded
- [ ] Repo URL and video URL added on the hackathon dashboard
