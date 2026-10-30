"""Fleet: racks, placement, queries across every registered unit, rack power, and rollout waves.

A single node manifest answers "what breaks in this node". A fleet answers the questions an
operator actually has to sign off on: which of our units carry this part or this firmware, which
racks go over budget if they are all upgraded, and in what order to change them so that no rack
loses too many units at once.

Power numbers here are **declared** draw (manifest and catalog), summed per rack. They are an
estimate of what is provisioned, not a measurement of what is drawn.
"""
from __future__ import annotations

import operator
import re
from types import SimpleNamespace

from vcm_ai import analyze
from vcm_ai.catalog import Catalog
from vcm_ai.rules_datacenter import domain_load, node_meta
from vcm_core import Repository, VcmError, now_iso
from vcm_parsers import design_from_revision

from .devices import SERIAL_RE, list_devices

RACK_KIND = "racks"
_OPS = {"<": operator.lt, "<=": operator.le, "=": operator.eq, "==": operator.eq,
        "!=": operator.ne, ">=": operator.ge, ">": operator.gt}
_FILTER = re.compile(r"^\s*([A-Za-z0-9_.:-]+)\s*(<=|>=|==|!=|<|>|=)\s*(.+?)\s*$")


# ---- racks and placement -------------------------------------------------------------
def define_rack(repo: Repository, name: str, power_budget_w: float | None = None,
                cooling_w: float | None = None, site: str = "", units: int | None = None) -> dict:
    if not SERIAL_RE.match(name):
        raise VcmError(f"invalid rack name: {name!r}")
    existing = repo.list_records(RACK_KIND)
    rec = next((r for r in existing if r["name"] == name), {"name": name, "created_at": now_iso()})
    rec.update({k: v for k, v in {"power_budget_w": power_budget_w, "cooling_w": cooling_w,
                                  "site": site, "units": units}.items() if v not in (None, "")})
    repo.save_record(RACK_KIND, name, rec)
    repo.audit.append("rack.define", name, {k: rec.get(k) for k in ("power_budget_w", "site")})
    return rec


def list_racks(repo: Repository) -> list[dict]:
    return repo.list_records(RACK_KIND)


def place_device(repo: Repository, serial: str, rack: str, position: int | None = None) -> dict:
    """Record where a unit is installed. Placement is not covered by passport signatures, since
    moving a unit does not change what it is."""
    dev = repo.load_record("devices", serial)
    if not any(r["name"] == rack for r in list_racks(repo)):
        define_rack(repo, rack)
    dev["location"] = {"rack": rack, "position": position}
    repo.save_record("devices", serial, dev)
    repo.audit.append("device.place", serial, {"rack": rack, "position": position})
    return dev


# ---- per-revision facts, cached ------------------------------------------------------
class _Facts:
    """Design, declared draw and firmware per revision; every unit on a revision shares them."""

    def __init__(self, repo: Repository):
        self.repo = repo
        self.catalog = Catalog.load(project_dir=repo.root)
        self._design: dict[str, object] = {}
        self._draw: dict[str, float | None] = {}

    def design(self, rev: str):
        if rev not in self._design:
            self._design[rev] = design_from_revision(self.repo, rev)
        return self._design[rev]

    def meta(self, rev: str) -> dict:
        return node_meta(self.design(rev))

    def draw(self, rev: str) -> float | None:
        """Declared draw of the node at ``rev`` across its power domains, or None if unknown."""
        if rev not in self._draw:
            design = self.design(rev)
            domains = (self.meta(rev).get("power") or {}).get("domains") or {}
            shim = SimpleNamespace(catalog=self.catalog)
            loads = [domain_load(shim, design, d, cfg) for d, cfg in domains.items()]
            loads = [x for x in loads if x]
            self._draw[rev] = sum(x["draw"] for x in loads) if loads else None
        return self._draw[rev]


def _version(v) -> tuple | str:
    s = str(v).strip()
    return tuple(int(x) for x in s.split(".")) if re.fullmatch(r"\d+(\.\d+)*", s) else s.lower()


def parse_filter(expr: str) -> tuple[str, str, str]:
    """``gpu_driver<535.104`` -> ``("gpu_driver", "<", "535.104")``."""
    m = _FILTER.match(expr)
    if not m:
        raise VcmError(f"cannot parse filter {expr!r}; use KEY<OP>VALUE, e.g. gpu_driver<535")
    return m.group(1), m.group(2), m.group(3)


def _firmware_ok(meta: dict, filters: list[tuple[str, str, str]]) -> bool:
    fw = meta.get("firmware") or {}
    for key, op, want in filters:
        if key not in fw:
            return False
        a, b = _version(fw[key]), _version(want)
        if type(a) is not type(b):
            a, b = str(fw[key]).lower(), str(want).lower()
        if not _OPS[op](a, b):
            return False
    return True


# ---- queries -------------------------------------------------------------------------
def fleet_query(repo: Repository, type_: str | None = None, model: str | None = None,
                mpn: str | None = None, firmware: list[str] | None = None,
                rack: str | None = None, release: str | None = None,
                facts: _Facts | None = None) -> list[dict]:
    """Units matching every given condition, with the components that matched.

    ``model`` and ``mpn`` are case-insensitive substrings; ``firmware`` entries are comparisons
    such as ``gpu_driver<535`` (dotted versions compare numerically).
    """
    facts = facts or _Facts(repo)
    filters = [parse_filter(f) for f in firmware or []]
    out = []
    for dev in list_devices(repo):
        loc = dev.get("location") or {}
        if rack and loc.get("rack") != rack:
            continue
        if release and dev["release"] != release:
            continue
        rev = dev["revision"]
        if filters and not _firmware_ok(facts.meta(rev), filters):
            continue
        comps = []
        if type_ or model or mpn:
            for ref, c in sorted(facts.design(rev).components.items()):
                if type_ and (c.lib_id or "").lower() != type_.lower():
                    continue
                if model and model.lower() not in c.value.lower():
                    continue
                if mpn and mpn.lower() not in (c.mpn or "").lower():
                    continue
                comps.append({"ref": ref, "model": c.value, "mpn": c.mpn})
            if not comps:
                continue
        out.append({"serial": dev["serial"], "release": dev["release"], "revision": rev,
                    "batch": dev.get("batch", ""), "rack": loc.get("rack"),
                    "position": loc.get("position"),
                    "firmware": facts.meta(rev).get("firmware") or {}, "components": comps})
    return out


def recall(repo: Repository, **criteria) -> dict:
    """Everything an incident needs in one place: affected units grouped by rack and release."""
    units = fleet_query(repo, **criteria)
    by_rack: dict[str, list[str]] = {}
    by_release: dict[str, list[str]] = {}
    for u in units:
        by_rack.setdefault(u["rack"] or "(unplaced)", []).append(u["serial"])
        by_release.setdefault(u["release"], []).append(u["serial"])
    return {"criteria": {k: v for k, v in criteria.items() if v}, "units": units,
            "count": len(units), "components": sum(len(u["components"]) for u in units),
            "by_rack": by_rack, "by_release": by_release, "generated_at": now_iso()}


# ---- rack power ----------------------------------------------------------------------
def rack_power(repo: Repository, upgrade_to: str | None = None, only_release: str | None = None,
               facts: _Facts | None = None) -> dict:
    """Declared draw per rack today and, with ``upgrade_to``, after moving units to that revision.

    ``only_release`` limits the upgrade to units currently on that release.
    """
    facts = facts or _Facts(repo)
    target = repo.resolve(upgrade_to) if upgrade_to else None
    racks = {r["name"]: r for r in list_racks(repo)}
    rows: dict[str, dict] = {}
    for dev in list_devices(repo):
        name = (dev.get("location") or {}).get("rack")
        if not name:
            continue
        row = rows.setdefault(name, {"rack": name, "units": [], "now_w": 0.0, "after_w": 0.0,
                                     "unknown": []})
        now = facts.draw(dev["revision"])
        moves = target and (only_release is None or dev["release"] == only_release)
        after = facts.draw(target) if moves else now
        row["units"].append(dev["serial"])
        if now is None or after is None:
            row["unknown"].append(dev["serial"])
            continue
        row["now_w"] += now
        row["after_w"] += after
    result = []
    for name in sorted(set(rows) | set(racks)):
        row = rows.get(name, {"rack": name, "units": [], "now_w": 0.0, "after_w": 0.0,
                              "unknown": []})
        budget = (racks.get(name) or {}).get("power_budget_w")
        row["budget_w"] = budget
        for key in ("now", "after"):
            w = row[f"{key}_w"]
            row[f"{key}_status"] = ("no budget" if not budget else "over" if w > budget
                                    else "tight" if w > 0.9 * budget else "ok")
        result.append(row)
    return {"upgrade_to": target, "only_release": only_release, "racks": result,
            "over_after": [r["rack"] for r in result if r["after_status"] == "over"],
            "note": "Declared draw from manifests and catalog, not measured consumption."}


# ---- rollout planning ----------------------------------------------------------------
def plan_rollout(repo: Repository, to: str, only_release: str | None = None,
                 max_per_rack: int = 1, wave_size: int = 10,
                 allow_critical: bool = False) -> dict:
    """Order a fleet change into waves.

    * Units whose change has a critical finding in the impact report are **blocked** (unless
      ``allow_critical``), with the findings named.
    * A wave takes at most ``max_per_rack`` units from any one rack, so a bad change cannot take
      a whole rack down at once, and at most ``wave_size`` units overall.
    * A unit whose upgrade would push its rack over its power budget is **deferred** rather than
      scheduled.
    """
    if max_per_rack < 1 or wave_size < 1:
        raise VcmError("max_per_rack and wave_size must be at least 1")
    facts = _Facts(repo)
    target = repo.resolve(to)
    racks = {r["name"]: r for r in list_racks(repo)}
    impacts: dict[str, dict] = {}
    blocked, deferred, pending = [], [], []
    for dev in list_devices(repo):
        if only_release and dev["release"] != only_release:
            continue
        if dev["revision"] == target:
            continue
        src = dev["revision"]
        if src not in impacts:
            rep = analyze(repo, src, target)
            impacts[src] = {"critical": [f"{f['id']} {f['title']}" for f in rep["findings"]
                                         if f["severity"] == "critical"]}
        crit = impacts[src]["critical"]
        if crit and not allow_critical:
            blocked.append({"serial": dev["serial"], "from": src, "critical": crit})
        else:
            pending.append(dev)

    # current declared draw per rack, updated as units are scheduled
    rack_w: dict[str, float] = {}
    for dev in list_devices(repo):
        name = (dev.get("location") or {}).get("rack")
        if name:
            rack_w[name] = rack_w.get(name, 0.0) + (facts.draw(dev["revision"]) or 0.0)

    queue: dict[str, list[dict]] = {}
    for dev in sorted(pending, key=lambda d: d["serial"]):
        queue.setdefault((dev.get("location") or {}).get("rack") or "(unplaced)", []).append(dev)
    waves: list[dict] = []
    while any(queue.values()):
        wave: list[dict] = []
        for name in sorted(queue):
            taken = 0
            while queue[name] and taken < max_per_rack and len(wave) < wave_size:
                dev = queue[name].pop(0)
                delta = (facts.draw(target) or 0.0) - (facts.draw(dev["revision"]) or 0.0)
                budget = (racks.get(name) or {}).get("power_budget_w")
                if budget and rack_w.get(name, 0.0) + delta > budget:
                    deferred.append({"serial": dev["serial"], "rack": name,
                                     "reason": f"rack would reach {rack_w[name] + delta:.0f} W "
                                               f"of {budget:g} W budget"})
                    continue
                rack_w[name] = rack_w.get(name, 0.0) + delta
                wave.append({"serial": dev["serial"], "rack": name, "from": dev["revision"]})
                taken += 1
        if not wave:
            break
        waves.append({"wave": len(waves) + 1, "units": wave})
    return {"to": target, "only_release": only_release, "max_per_rack": max_per_rack,
            "wave_size": wave_size, "waves": waves, "blocked": blocked, "deferred": deferred,
            "scheduled": sum(len(w["units"]) for w in waves)}
