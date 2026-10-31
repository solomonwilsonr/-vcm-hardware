"""The fleet of record, on Appwrite TablesDB.

A unit is a physical machine. It is registered by whoever is standing in front of it, and asked
about later by someone else entirely: an operator planning a rollout, a technician scanning the QR
sticker on the front panel, an engineer running a recall query after a bad batch of accelerators.
Those are different people on different machines reasoning about the same hardware, so the registry
cannot live in one engineer's ``.vcm`` folder. With a per-clone registry, ``vcm device affected``
silently answers about a subset of the fleet, which is the one question it must never get wrong.

This store makes TablesDB authoritative for the two kinds that describe hardware — ``devices`` and
``racks`` — and demotes the local JSON files to a cache. The split in behaviour is deliberate:

* **Writes fail closed.** If the fleet of record is unreachable, registering a unit raises. A unit
  recorded only on the laptop that happened to register it is precisely the bug being fixed here,
  so a half-written registration is worse than a loud failure.
* **Reads degrade open.** Reads fall back to the local mirror when the service is unreachable, but
  the store remembers that it did (:attr:`degraded`) and every caller that reports to a human says
  so. Stale answers are useful offline; silently stale answers are not.

Everything else — releases, pull requests, agent proposals — stays in the checkout, because it is
repository-scoped and travels with the files it describes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from vcm_core import FileRecordStore, RecordStore, check_name
from vcm_graph.backends.appwrite_backend import (
    AppwriteConfig,
    AppwriteConfigError,
    _as_dict,
    _row_id,
    _rows,
    tables_db,
)

DEVICES = "vcm_devices"
RACKS = "vcm_racks"
PAGE = 100

#: kind -> (table, row-id prefix). Only these two kinds are shared; anything else is a programming
#: error rather than a configuration one.
TABLES: dict[str, tuple[str, str]] = {"devices": (DEVICES, "d"), "racks": (RACKS, "r")}

# key -> (column type, size, required). ``doc`` carries the whole record verbatim so the store
# never has to be migrated when a record grows a field; the other columns exist so that the
# questions an operator actually asks ("every unit on release R1") are answered by an index rather
# than by paging the entire fleet back to the client.
DEVICE_COLUMNS: dict[str, tuple[str, int, bool]] = {
    "name": ("varchar", 64, True),
    "release": ("varchar", 64, False),
    "revision": ("varchar", 64, False),
    "batch": ("varchar", 64, False),
    "rack": ("varchar", 64, False),
    "doc": ("text", 0, True),
}

RACK_COLUMNS: dict[str, tuple[str, int, bool]] = {
    "name": ("varchar", 64, True),
    "site": ("varchar", 64, False),
    "doc": ("text", 0, True),
}

DEVICE_INDEXES: dict[str, tuple[list[str], list[int]]] = {
    "device_name": (["name"], [64]),
    "device_release": (["release"], [64]),
    "device_revision": (["revision"], [64]),
    "device_rack": (["rack"], [64]),
}

RACK_INDEXES: dict[str, tuple[list[str], list[int]]] = {
    "rack_name": (["name"], [64]),
    "rack_site": (["site"], [64]),
}

SCHEMA = {DEVICES: (DEVICE_COLUMNS, DEVICE_INDEXES), RACKS: (RACK_COLUMNS, RACK_INDEXES)}

#: set to "1" to keep the fleet in the checkout even when Appwrite is configured
ENV_LOCAL = "VCM_FLEET_LOCAL"


def _facets(kind: str, name: str, data: dict) -> dict:
    """The indexed columns for one record. Values are clipped to their column width."""
    if kind == "devices":
        loc = data.get("location") or {}
        row = {"name": name, "release": data.get("release", ""),
               "revision": data.get("revision", ""), "batch": data.get("batch", ""),
               "rack": (loc.get("rack") or "")}
    else:
        row = {"name": name, "site": data.get("site", "")}
    return {k: str(v)[:64] for k, v in row.items()}


class FleetUnreachable(RuntimeError):
    """The fleet of record could not be reached for a write that must not be lost."""


class AppwriteRecordStore(RecordStore):
    """Shared units and racks in TablesDB, mirrored to ``cache`` for offline reads.

    ``service`` may be injected for testing; otherwise it is built from ``cfg``. Kinds outside
    :data:`TABLES` are delegated to the local store untouched.
    """

    name = "appwrite"

    def __init__(self, cfg: AppwriteConfig | None = None, service=None,
                 cache: RecordStore | None = None, base: str | Path | None = None):
        self.cfg = cfg or AppwriteConfig()
        self.db = self.cfg.database
        self.svc = service if service is not None else tables_db(self.cfg)
        if cache is None and base is None:
            raise ValueError("AppwriteRecordStore needs a local cache directory or store")
        self.cache = cache or FileRecordStore(base)
        #: reason the last read fell back to the cache, or None while the service is answering
        self.degraded: str | None = None

    # ---- plumbing --------------------------------------------------
    @staticmethod
    def _q():
        from appwrite.query import Query
        return Query

    def _table(self, kind: str) -> tuple[str, str]:
        if kind not in TABLES:
            raise KeyError(kind)
        return TABLES[kind]

    def _id(self, kind: str, name: str) -> str:
        _, prefix = self._table(kind)
        return _row_id(prefix, kind, name)

    def _list_all(self, table: str, queries: list[str]) -> list[dict]:
        Query = self._q()
        out, offset = [], 0
        while True:
            page = self.svc.list_rows(self.db, table,
                                      queries=[*queries, Query.limit(PAGE), Query.offset(offset)])
            rows = _rows(page)
            out.extend(rows)
            if len(rows) < PAGE:
                return out
            offset += len(rows)

    @staticmethod
    def _doc(row: dict) -> dict:
        return json.loads(row.get("doc") or "{}")

    # ---- RecordStore ------------------------------------------------
    def save(self, kind: str, name: str, data: dict) -> Path:
        check_name(kind, name)
        if kind not in TABLES:
            return self.cache.save(kind, name, data)
        table, _ = self._table(kind)
        row = {"$id": self._id(kind, name), **_facets(kind, name, data),
               "doc": json.dumps(data, sort_keys=True)}
        try:
            self.svc.upsert_rows(self.db, table, rows=[row])
        except Exception as exc:
            # Fail closed: see the module docstring. The cache is deliberately left untouched so
            # that a later read does not present a unit the fleet has never heard of.
            raise FleetUnreachable(
                f"could not write {kind[:-1]} {name!r} to the fleet of record "
                f"({self.cfg.endpoint}): {exc}") from exc
        self.degraded = None
        return self.cache.save(kind, name, data)   # mirror, for offline reads

    def load(self, kind: str, name: str) -> dict | None:
        check_name(kind, name)
        if kind not in TABLES:
            return self.cache.load(kind, name)
        table, _ = self._table(kind)
        Query = self._q()
        try:
            rows = self._list_all(table, [Query.equal("name", name)])
        except Exception as exc:
            return self._fallback(f"load {kind[:-1]} {name!r}", exc,
                                  lambda: self.cache.load(kind, name))
        self.degraded = None
        return self._doc(rows[0]) if rows else None

    def list(self, kind: str) -> list[dict]:
        if kind not in TABLES:
            return self.cache.list(kind)
        table, _ = self._table(kind)
        try:
            rows = self._list_all(table, [])
        except Exception as exc:
            return self._fallback(f"list {kind}", exc, lambda: self.cache.list(kind))
        self.degraded = None
        return [self._doc(r) for r in sorted(rows, key=lambda r: r.get("name", ""))]

    def has(self, kind: str, name: str) -> bool:
        check_name(kind, name)
        if kind not in TABLES:
            return self.cache.has(kind, name)
        return self.load(kind, name) is not None

    def _fallback(self, what: str, exc: Exception, read):
        self.degraded = f"{what} fell back to the local cache: {exc}"
        return read()

    # ---- fleet-wide queries the local store cannot answer -----------
    def by_release(self, release: str) -> list[dict]:
        """Every unit on a release, filtered in TablesDB rather than client-side."""
        Query = self._q()
        return [self._doc(r) for r in self._list_all(DEVICES, [Query.equal("release", release)])]

    def by_rack(self, rack: str) -> list[dict]:
        Query = self._q()
        return [self._doc(r) for r in self._list_all(DEVICES, [Query.equal("rack", rack)])]

    def close(self) -> None:
        pass


# ---- selection -----------------------------------------------------
def fleet_is_shared(cfg: AppwriteConfig | None = None) -> bool:
    """Whether this machine should use the shared fleet.

    Shared when Appwrite is fully configured and ``VCM_FLEET_LOCAL`` is unset, so that a developer
    with no credentials — and the test suite — keep the old on-disk behaviour with no flags.
    """
    if os.environ.get(ENV_LOCAL, "").strip() not in ("", "0", "false", "no"):
        return False
    return (cfg or AppwriteConfig()).complete


def open_record_store(repo_dir: str | Path, cfg: AppwriteConfig | None = None,
                      service=None) -> RecordStore:
    """The record store for a repository: shared when configured, local otherwise."""
    cfg = cfg or AppwriteConfig()
    if service is None and not fleet_is_shared(cfg):
        return FileRecordStore(repo_dir)
    return AppwriteRecordStore(cfg, service=service, base=repo_dir)


def open_repo(root: str | Path = ".", cfg: AppwriteConfig | None = None, service=None):
    """``Repository.discover`` with the fleet store already attached."""
    from vcm_core import Repository
    repo = Repository.discover(root)
    store = open_record_store(repo.dir, cfg, service)
    if store.name != repo.records.name:
        repo.records = store
    return repo


# ---- provisioning and health ---------------------------------------
_CREATORS = {"varchar": "create_varchar_column", "text": "create_text_column"}


def provision(cfg: AppwriteConfig | None = None, service=None) -> dict:
    """Create the fleet tables, columns and indexes. Idempotent, like the graph's."""
    cfg = cfg or AppwriteConfig()
    svc = service if service is not None else tables_db(cfg)
    from appwrite.enums.tables_db_index_type import TablesDBIndexType
    done: dict[str, list[str]] = {"created": [], "existed": []}

    def step(label: str, fn) -> None:
        try:
            fn()
            done["created"].append(label)
        except Exception as exc:
            if "already exists" in str(exc).lower() or "409" in str(exc):
                done["existed"].append(label)
            else:
                raise

    # Ask whether the database is there rather than inferring it from a failed create: the error
    # for "it already exists" and for "your plan is out of databases" are both 403s, and VCM
    # shares one database with the graph tables, so on a real project it usually does exist.
    label = f"database:{cfg.database}"
    try:
        svc.list_tables(cfg.database)
        done["existed"].append(label)
    except Exception:
        step(label, lambda: svc.create(cfg.database, "VCM"))
    for table, (columns, indexes) in SCHEMA.items():
        step(f"table:{table}", lambda t=table: svc.create_table(cfg.database, t, t))
        for key, (kind, size, required) in columns.items():
            def make(t=table, k=key, kind=kind, size=size, required=required):
                fn = getattr(svc, _CREATORS[kind])
                if kind == "varchar":
                    fn(cfg.database, t, k, size, required)
                else:
                    fn(cfg.database, t, k, required)
            step(f"column:{table}.{key}", make)
        for name, (cols, lengths) in indexes.items():
            step(f"index:{name}", lambda n=name, t=table, c=cols, ln=lengths: svc.create_index(
                cfg.database, t, n, TablesDBIndexType.KEY, c, lengths=ln))
    return done


def health(cfg: AppwriteConfig | None = None, service=None) -> dict:
    """Whether the fleet of record is reachable, provisioned, and how big it is."""
    cfg = cfg or AppwriteConfig()
    if not cfg.complete:
        return {"ok": False, "configured": False, "shared": False, "missing": cfg.missing(),
                "detail": "fleet is local to this checkout; set the VCM_APPWRITE_* variables "
                          "to share it"}
    try:
        svc = service if service is not None else tables_db(cfg)
        listing = svc.list_tables(cfg.database)
        raw = listing["tables"] if isinstance(listing, dict) else getattr(listing, "tables", [])
        names = {getattr(t, "id", None) or _as_dict(t).get("$id") for t in raw}
        missing = [t for t in SCHEMA if t not in names]
        out = {"ok": not missing, "configured": True, "shared": fleet_is_shared(cfg),
               "endpoint": cfg.endpoint, "database": cfg.database, "missing_tables": missing}
        if not missing:
            store = AppwriteRecordStore(cfg, service=svc, cache=FileRecordStore("."))
            out["units"] = len(store.list("devices"))
            out["racks"] = len(store.list("racks"))
        return out
    except Exception as exc:
        return {"ok": False, "configured": True, "shared": False, "endpoint": cfg.endpoint,
                "error": str(exc)}


__all__ = ["AppwriteRecordStore", "FleetUnreachable", "open_record_store", "open_repo",
           "fleet_is_shared", "provision", "health", "AppwriteConfigError", "SCHEMA",
           "DEVICES", "RACKS"]
