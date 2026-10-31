"""The component knowledge base behind every ``rule_based`` finding.

Three things matter here, and they are all about honesty rather than cleverness:

* **Layering.** A bundled starter catalog ships with VCM, a user catalog lives in ``~/.vcm/catalog``
  and a project catalog in ``vcm-catalog/`` next to the design. Later layers win, so a team can
  correct a bundled value without forking VCM, and the correction is itself version-controlled.
* **Attribution.** Every part records where its numbers came from, and a part may attribute
  individual fields through ``sources``. A heuristic that cites its datasheet page is reviewable;
  one that does not is folklore.
* **Coverage.** The catalog will never know every part. What it must never do is stay silent about
  that, because a reader cannot otherwise tell "the rules passed" from "no rules ran".
"""
from __future__ import annotations

import csv
import json
import os
from importlib import resources
from pathlib import Path

#: fields a part may declare; anything else is carried through untouched
NUMERIC_FIELDS = ("iout_max_ma", "vin_max", "vout", "dropout_v", "min_cout_uf", "iq_ua",
                  "rth_ja_c_per_w", "tj_max_c", "i_typ_ma", "i_peak_ma", "supply_vmin",
                  "supply_vmax", "vmax",
                  # datacenter node fields
                  "tdp_w", "watts", "pcie_gen", "pcie_lanes", "memory_gb", "capacity_gb",
                  "slot_width", "memory_channels", "max_memory_mts", "speed_gbps")
#: every field a rule may read; a part is ``reviewed`` only when each one it carries is verified
REVIEWABLE = NUMERIC_FIELDS + ("min_driver", "min_cuda", "min_bios", "min_bmc",
                               "power_connector", "memory_type", "socket", "form_factor",
                               "output_cap", "package")

USER_CATALOG = Path(os.environ.get("VCM_CATALOG_HOME", Path.home() / ".vcm" / "catalog"))
PROJECT_CATALOG_DIR = "vcm-catalog"


class CatalogError(ValueError):
    """A catalog file could not be read or does not describe parts."""


def _normalise(part: dict, origin: str) -> dict:
    """Validate the shape of one part entry and record where it came from."""
    if not isinstance(part, dict) or not part.get("match"):
        raise CatalogError(f"{origin}: every part needs a non-empty 'match' list")
    out = dict(part)
    out["match"] = [str(m) for m in part["match"]]
    for key in NUMERIC_FIELDS:
        if key in out and out[key] is not None:
            try:
                out[key] = float(out[key])
            except (TypeError, ValueError) as exc:
                raise CatalogError(f"{origin}: {key} is not a number: {out[key]!r}") from exc
    out.setdefault("source", "")
    out.setdefault("sources", {})
    # reviewed means "a human checked these numbers against the datasheet"; default to no
    out.setdefault("reviewed", False)
    out["_origin"] = origin
    return out


def read_file(path: str | Path) -> list[dict]:
    """Parse one catalog file. JSON (``{"parts": [...]}`` or a bare list) or CSV."""
    p = Path(path)
    text = p.read_text()
    if p.suffix.lower() == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
        parts = []
        for row in rows:
            part = {k: v for k, v in row.items() if k and v not in ("", None)}
            match = part.pop("match", "") or part.pop("mpn", "")
            part["match"] = [m.strip() for m in str(match).split("|") if m.strip()]
            parts.append(part)
    else:
        data = json.loads(text)
        parts = data["parts"] if isinstance(data, dict) else data
    if not isinstance(parts, list):
        raise CatalogError(f"{p}: expected a list of parts")
    return [_normalise(part, str(p)) for part in parts]


class Catalog:
    """Layered component properties, newest layer winning on a shared ``match`` key."""

    def __init__(self, parts: list[dict] | None = None):
        self.parts: list[dict] = list(parts or [])
        self._cache: dict[tuple[str, str], dict | None] = {}

    # ---- construction ----------------------------------------------
    #: bundled files, in increasing precedence
    BUNDLED_FILES = ("data/catalog-starter.json", "data/catalog-datacenter.json",
                     "data/components.json")

    @classmethod
    def bundled(cls) -> Catalog:
        """The starter catalog, then the curated demo entries, which win on a shared match key."""
        cat = cls()
        for name in cls.BUNDLED_FILES:
            text = resources.files("vcm_ai").joinpath(name).read_text()
            origin = "bundled:" + name.rsplit("/", 1)[-1]
            cat.merge([_normalise(part, origin) for part in json.loads(text)["parts"]])
        return cat

    @classmethod
    def load(cls, project_dir: str | Path | None = None, user_dir: str | Path | None = None,
             bundled: bool = True) -> Catalog:
        """Bundled, then the user catalog, then the project catalog. Later layers win."""
        cat = cls.bundled() if bundled else cls()
        for folder in (Path(user_dir) if user_dir else USER_CATALOG,
                       Path(project_dir) / PROJECT_CATALOG_DIR if project_dir else None):
            if folder and folder.is_dir():
                for f in sorted(folder.iterdir()):
                    if f.suffix.lower() in (".json", ".csv"):
                        cat.merge(read_file(f))
        return cat

    def merge(self, parts: list[dict]) -> int:
        """Add parts, replacing any existing entry with the same primary match key."""
        for part in parts:
            key = part["match"][0].upper()
            for i, existing in enumerate(self.parts):
                if existing["match"][0].upper() == key:
                    self.parts[i] = part
                    break
            else:
                self.parts.append(part)
        self._cache.clear()
        return len(parts)

    # ---- lookup ----------------------------------------------------
    def find(self, mpn: str = "", value: str = "") -> dict | None:
        """Longest-prefix match on MPN first, then value. Longest wins, so ``MCP1700-3302``
        beats a generic ``MCP1700`` entry."""
        cache_key = (mpn or "", value or "")
        if cache_key in self._cache:
            return self._cache[cache_key]
        best, best_len = None, -1
        for key in [s.upper() for s in (mpn, value) if s]:
            for part in self.parts:
                for m in part["match"]:
                    if key.startswith(m.upper()) and len(m) > best_len:
                        best, best_len = part, len(m)
            if best is not None:
                break
        self._cache[cache_key] = best
        return best

    def lookup(self, component) -> dict | None:
        return self.find(getattr(component, "mpn", ""), getattr(component, "value", ""))

    # ---- coverage --------------------------------------------------
    def coverage(self, design, refs: list[str] | None = None,
                 declared: set[str] | frozenset[str] | None = None) -> dict:
        """Which components of a design the rules can actually reason about.

        Three things are not catalog gaps and are reported separately, because counting them as
        gaps would overstate the problem:

        * **passives** (``C``/``R``/``D``/``L``… prefixes) — the rules never need an entry for them;
        * **declared** components — ``vcm.json`` states their load directly, so the catalog is not
          the source of truth for them;
        * everything else without an entry is a genuine **unknown**, and is disclosed as such.
        """
        chosen = refs if refs is not None else sorted(design.components)
        declared = set(declared or ())
        known, unknown, passive, declared_hit = [], [], [], []
        for ref in chosen:
            c = design.components.get(ref)
            if c is None:
                continue
            if self.lookup(c) is not None:
                known.append(ref)
            elif ref in declared:
                declared_hit.append(ref)
            elif ref[:1].upper() in ("C", "R", "D", "L", "F", "Y", "T"):
                passive.append(ref)
            else:
                unknown.append(ref)
        considered = len(known) + len(unknown)
        return {"known": known, "unknown": unknown, "passive": passive,
                "declared": declared_hit, "considered": considered,
                "parts_in_catalog": len(self.parts),
                "ratio": round(len(known) / considered, 3) if considered else 1.0}

    def describe(self) -> dict:
        """Where this catalog's parts came from, by layer, and how many are datasheet-reviewed."""
        layers: dict[str, int] = {}
        for part in self.parts:
            origin = part.get("_origin", "unknown")
            layers[origin] = layers.get(origin, 0) + 1
        return {"parts": len(self.parts), "layers": layers,
                "reviewed": sum(1 for p in self.parts if p.get("reviewed"))}


def field_source(part: dict, key: str) -> dict:
    """Where one value of a part came from: its own attribution if it has one, else the part's.

    ``sources`` maps a field to a string (a citation) or to
    ``{"source": ..., "verified_by": ..., "verified_at": ...}``. Only the latter counts as checked.
    """
    raw = (part.get("sources") or {}).get(key)
    if isinstance(raw, dict):
        return {"source": raw.get("source", ""), "verified_by": raw.get("verified_by", ""),
                "verified_at": raw.get("verified_at", "")}
    return {"source": raw or part.get("source", ""), "verified_by": "", "verified_at": ""}


def unverified_fields(part: dict) -> list[str]:
    return [k for k in REVIEWABLE if part.get(k) not in (None, "")
            and not field_source(part, k)["verified_by"]]


def review_part(part: dict, values: dict, source: str, by: str, at: str,
                confirm: list[str] | tuple[str, ...] = ()) -> dict:
    """A copy of ``part`` with ``values`` set and those fields (plus ``confirm``) attributed to a
    named human checking a named source. ``reviewed`` becomes true only once every rule-relevant
    field the part carries has been checked this way."""
    if not source or not by:
        raise CatalogError("a review needs both a source citation and a reviewer")
    out = {k: v for k, v in part.items() if not k.startswith("_")}
    out["sources"] = dict(out.get("sources") or {})
    for key in [*values, *confirm]:
        if key not in REVIEWABLE:
            raise CatalogError(f"{key} is not a reviewable field ({', '.join(REVIEWABLE)})")
        if key in values:
            out[key] = values[key]
        elif out.get(key) in (None, ""):
            raise CatalogError(f"cannot confirm {key}: the part has no value for it")
        out["sources"][key] = {"source": source, "verified_by": by, "verified_at": at}
    norm = _normalise(out, "review")
    out = {k: v for k, v in norm.items() if k != "_origin"}
    out["reviewed"] = not unverified_fields(out)
    return out


_DEFAULT: Catalog | None = None


def default_catalog() -> Catalog:
    """Process-wide catalog built from the bundled data plus the user catalog."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Catalog.load()
    return _DEFAULT


def reset_default() -> None:
    """Drop the cached catalog (used by tests that write catalog files)."""
    global _DEFAULT
    _DEFAULT = None
