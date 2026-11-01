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

**Lead with the dashboard, not the terminal, and keep a TablesDB view on screen.** The CLI is the
engine; the dashboard is what a judge can read in ninety seconds. Have two windows ready: VCM at
`http://127.0.0.1:8080` and the Appwrite console on `vcm_devices`.

```bash
vcm serve --insecure            # dashboard on 127.0.0.1:8080
```

1. **(0:00) The question.** Open **Compare**. "Someone proposes swapping eight A100s for H100 NVL."
   Pick v1.0 -> v1.1, hit Compare. Read the verdict line aloud: what changed, and what it affects.
2. **(0:15) The trust claim.** Point at the chips: `VERIFIED`, `RULE-BASED`, `SPECULATIVE`. "Every
   statement is labelled by how much VCM actually knows. A guess is never shown as a fact." Open
   **What VCM knows** and show a part the catalog does not have — VCM says which checks it skipped.
3. **(0:35) Appwrite is the fleet of record.** Open **Fleet**. These rows are read live from
   TablesDB. Put the Appwrite console beside it on `vcm_devices` — same rows, same ids.
4. **(0:50) Why it has to be a shared database.** Run `bash examples/demo-fleet.sh`. Two separate
   checkouts register units into one fleet; the duplicate from the second clone is rejected by the
   deterministic row id. Refresh **Fleet**: both checkouts' units are there. "A per-checkout
   registry answers the recall question about a subset of the fleet. That is the bug TablesDB
   fixes, and indexes on `release` and `rack` make the recall an index lookup, not a scan."
5. **(1:10) The recall, answered fleet-wide.** Still on **Fleet**, query the bad release and show
   every physical unit carrying it.
6. **(1:20) The guardrail.** Open **Agents**: an agent can only propose; a named person approves.
7. **(1:30) Honesty.** "Offline by default, signed releases, and the README says plainly what is
   not built yet."

Keep the terminal for steps 4 only. Everything else is the dashboard, so the database is on screen
for roughly half the video.

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
