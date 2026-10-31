# TODO

Everything left to do, in the order it matters. Tick items off as you go.
Items marked **(you)** need a person, an account or hardware that an agent does not have.

## 1. Hackathon submission (due Oct 31, 11:59 PM PT)

See [docs/hackathon.md](docs/hackathon.md) for the pitch and the 90-second demo script.

- [ ] **(you)** Make the GitHub repository public
- [ ] **(you)** `git pull origin claude/festive-faraday-q76lkf` on the machine with your Appwrite project
- [ ] **(you)** `python3 -m venv .venv && . .venv/bin/activate && make install`
- [ ] **(you)** `cp .env.example .env` and fill in the four `VCM_APPWRITE_*` values
      (API key scopes are listed in `.env.example`)
- [ ] **(you)** `make hackathon` runs clean (checks credentials, provisions, pushes graph, runs fleet demo)
- [ ] **(you)** Screenshot `vcm_devices`, `vcm_nodes` and `vcm_edges` in the Appwrite console
- [ ] **(you)** Record the demo video (about 90 seconds, script in `docs/hackathon.md`) and upload it
- [ ] **(you)** Sign in on the hackathon dashboard and add the repo URL and the video URL
- [ ] **(you)** Submit before the deadline, and confirm the dashboard shows it as submitted
- [ ] Optional: open a pull request for `claude/festive-faraday-q76lkf` if you want it on the default branch

## 2. Verify what has not been run live

- [ ] **(you)** Run `vcm graph push` and `vcm graph query --backend appwrite` against the real project
- [ ] **(you)** Run `vcm catalog provision` and part search on VectorsDB
      (needs the `embeddings.write` and `vectors.*` scopes). Never run live so far
- [ ] **(you)** If anything fails, note the error in `docs/hackathon.md` under "What has been run"

## 3. Make the knowledge real

- [ ] **(you)** Check the 31 values in `docs/review/catalog-review-worksheet.csv` against datasheets.
      Fill in `decision`, `datasheet_source` (document and page) and `checked_by` (your own name)
- [ ] Run `vcm catalog review --from-csv docs/review/catalog-review-worksheet.csv`
- [ ] Regenerate the worksheet for the next most-used parts (`vcm catalog priority`, `worksheet`)
- [ ] **(you)** Capture real Redfish and `nvidia-smi` output from a real server
- [ ] Scrub it with `vcm inventory sanitize`, then add it under `tests/captures/<name>/`
- [ ] **(you)** Run `vcm inventory fetch-redfish` against a real BMC
- [ ] **(you)** Run `vcm fleet netbox --url ...` against a real NetBox
- [ ] **(you)** Run `vcm eval run` against a live agent (evals have only met scripted answerers)
- [ ] **(you)** Connect a real MCP client (for example Claude) to `vcm mcp`, file a proposal, and approve it on the dashboard's Agents page

## 4. Housekeeping

- [ ] Update the stale test count in `CLAUDE.md` (it says 306; the suite currently reports 352 passed, 1 skipped)
- [ ] Update the implemented-vs-planned table in `README.md` whenever scope changes
- [ ] Add a competitor comparison to `docs/` (DCops, OpenCHAMI, Ralph, openDCIM, KiRI and others
      turned up in a quick search; none were studied in depth)
- [ ] Keep `make lint test` green before every commit
- [ ] Decide whether to merge or close the older branches (`claude/sharp-cerf-o4bw7x` and the others on GitHub)
- [ ] Decide what to do with open PR #2 and closed, unmerged PR #1

## 5. Not built yet (marked Planned in the README)

Scope each one before starting. Do not describe any of them as working until they are.

- [ ] Web app (`apps/web`)
- [ ] Desktop app (`apps/desktop`)
- [ ] FreeCAD integration (`integrations/freecad`)
- [ ] KiCad integration (`integrations/kicad`)
- [ ] Git bridge (`integrations/git`)
- [ ] Remote transport beyond a shared directory (S3 or Git)
- [x] Dashboard page for agent proposals (Agents page; still needs a live agent client run)
- [ ] Hardware pull requests: merge action, identity and permissions (currently a local review record)
- [ ] "GPU Git" for GPUOS/VACE (`docs/gpu-git.md` is a scope document only)

## 6. Board domain (still works, no longer the product)

- [ ] **(you)** Build the sample ESP32-C3 board in real KiCad 9 and replace the hand-written fixtures
- [ ] Keep the hand-made fixtures as a separate synthetic set
- [ ] Add fixtures for KiCad 6, 7, 8 and 9 output
- [ ] Add a KiCad step to CI so `kicad-cli` DRC/ERC actually runs
- [ ] Schematic net tracing (geometric connectivity over `.kicad_sch`), then cross-check against PCB nets
- [ ] Decide whether to keep the board domain or retire it. Retiring it removes working, tested features
