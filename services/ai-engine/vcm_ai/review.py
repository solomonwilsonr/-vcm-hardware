"""Making catalog review fast, without making it fake.

``reviewed`` means a named person checked a value against a cited source. Nothing here can set it
on anyone's behalf: this module ranks which parts are worth a person's time, writes the sheet they
fill in, and loads the sheet back only when every row they decided on names who decided and what
they checked it against.

* ``priority`` ranks parts by how many of *your* units carry them (falling back to a revision when
  no units are registered), so the first afternoon of reviewing covers the numbers your fleet
  actually leans on.
* ``worksheet`` writes one row per unverified value, in that order.
* ``apply_worksheet`` validates the whole sheet first and applies it all or not at all.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict

from vcm_core import Repository
from vcm_parsers import design_from_revision

from .catalog import (
    REVIEWABLE,
    Catalog,
    CatalogError,
    field_source,
    review_part,
    unverified_fields,
)

COLUMNS = ["part", "type", "field", "value", "current_source", "units", "decision",
           "corrected_value", "datasheet_source", "checked_by", "notes"]
CONFIRM = {"ok", "confirm", "yes"}
CORRECT = {"correct", "fix"}


def _key(part: dict) -> str:
    return part["match"][0]


def usage(repo: Repository, catalog: Catalog, revisions: list[str] | None = None) -> dict:
    """``{part key: {"units", "components"}}`` over registered units, or over ``revisions``.

    A unit counts once per part however many of that part it carries; ``components`` counts them.
    """
    weights: dict[str, int] = {}
    devices = repo.list_records("devices")
    if devices:
        for d in devices:
            weights[d["revision"]] = weights.get(d["revision"], 0) + 1
        source = "units"
    else:
        for r in revisions or ["HEAD"]:
            weights[repo.resolve(r)] = 1
        source = "revisions"
    out: dict[str, dict] = defaultdict(lambda: {"units": 0, "components": 0})
    for rev, n in weights.items():
        seen: dict[str, int] = defaultdict(int)
        for c in design_from_revision(repo, rev).components.values():
            part = catalog.lookup(c)
            if part is not None:
                seen[_key(part)] += 1
        for k, count in seen.items():
            out[k]["units"] += n
            out[k]["components"] += count * n
    out["_source"] = source  # type: ignore[assignment]
    return out


def priority(repo: Repository, catalog: Catalog, revisions: list[str] | None = None,
             type_: str | None = None, include_unused: bool = False) -> list[dict]:
    use = usage(repo, catalog, revisions)
    source = use.pop("_source")
    rows = []
    for part in catalog.parts:
        if type_ and part.get("type") != type_:
            continue
        u = use.get(_key(part), {"units": 0, "components": 0})
        if not (u["units"] or include_unused):
            continue
        carried = [k for k in REVIEWABLE if part.get(k) not in (None, "")]
        left = unverified_fields(part)
        rows.append({"part": _key(part), "type": part.get("type", ""), "units": u["units"],
                     "components": u["components"], "fields": len(carried),
                     "unverified": left, "verified": len(carried) - len(left),
                     "reviewed": bool(part.get("reviewed")), "counted_by": source})
    rows.sort(key=lambda r: (-r["units"], -r["components"], -len(r["unverified"]), r["part"]))
    return rows


def worksheet(repo: Repository, catalog: Catalog, revisions: list[str] | None = None,
              type_: str | None = None, top: int | None = None) -> str:
    """CSV for a reviewer: one row per unverified value of the most-used parts."""
    ranked = [r for r in priority(repo, catalog, revisions, type_) if r["unverified"]]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in ranked[:top]:
        part = catalog.find(r["part"], r["part"])
        for field in r["unverified"]:
            w.writerow([r["part"], r["type"], field, part[field],
                        field_source(part, field)["source"], r["units"], "", "", "", "", ""])
    return buf.getvalue()


def _same(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def apply_worksheet(catalog: Catalog, text: str, at: str) -> dict:
    """Validate every row the reviewer decided on, then apply them all, or raise with every error.

    Rows with a blank ``decision`` are skipped. ``ok`` confirms the value as printed; ``correct``
    replaces it with ``corrected_value``. A row needs ``checked_by`` (a person: never an agent) and
    ``datasheet_source``. Confirming is refused if the catalog's value changed after the sheet was
    written, since that would verify a number nobody looked at.
    """
    rows = list(csv.DictReader(io.StringIO(text)))
    missing = [c for c in ("part", "field", "decision") if rows and c not in rows[0]]
    if missing:
        raise CatalogError(f"worksheet is missing column(s): {', '.join(missing)}")
    errors, plan, skipped = [], defaultdict(lambda: {"values": {}, "confirm": []}), 0
    for n, row in enumerate(rows, start=2):  # line 1 is the header
        decision = (row.get("decision") or "").strip().lower()
        if not decision:
            skipped += 1
            continue
        where = f"line {n} ({row.get('part')}.{row.get('field')})"
        part = catalog.find(row.get("part", ""), row.get("part", ""))
        field = (row.get("field") or "").strip()
        by = (row.get("checked_by") or "").strip()
        src = (row.get("datasheet_source") or "").strip()
        if part is None:
            errors.append(f"{where}: no such part in the catalog")
            continue
        if field not in REVIEWABLE or part.get(field) in (None, ""):
            errors.append(f"{where}: {field!r} is not a reviewable field this part carries")
            continue
        if not by or by.lower().startswith("agent:") or by.lower() in ("agent", "ai", "claude"):
            errors.append(f"{where}: checked_by must name the person who checked it")
            continue
        if len(src) < 4:
            errors.append(f"{where}: datasheet_source must cite what was checked "
                          "(document, revision, page)")
            continue
        if decision in CONFIRM:
            if not _same(row.get("value"), part[field]):
                errors.append(f"{where}: the catalog now says {part[field]!r}, not "
                              f"{row.get('value')!r}; regenerate the worksheet")
                continue
            kind = "confirm"
        elif decision in CORRECT:
            if (row.get("corrected_value") or "").strip() == "":
                errors.append(f"{where}: decision 'correct' needs corrected_value")
                continue
            kind = "correct"
        else:
            errors.append(f"{where}: decision {decision!r} is not one of ok, correct, or blank")
            continue
        slot = plan[(_key(part), by, src)]
        if kind == "confirm":
            slot["confirm"].append(field)
        else:
            slot["values"][field] = row["corrected_value"].strip()
    if errors:
        raise CatalogError(f"{len(errors)} problem(s), nothing applied:\n  " + "\n  ".join(errors))

    working = Catalog(list(catalog.parts))
    done: dict[str, dict] = {}
    confirmed = corrected = 0
    for (pkey, by, src), slot in plan.items():
        current = working.find(pkey, pkey)
        reviewed = review_part(current, slot["values"], src, by, at, slot["confirm"])
        working.merge([reviewed])
        done[pkey] = reviewed
        confirmed += len(slot["confirm"])
        corrected += len(slot["values"])
    return {"parts": list(done.values()), "confirmed": confirmed, "corrected": corrected,
            "skipped": skipped,
            "now_reviewed": [p["match"][0] for p in done.values() if p["reviewed"]],
            "still_unverified": {p["match"][0]: unverified_fields(p) for p in done.values()
                                 if not p["reviewed"]}}
