"""Appwrite TablesDB backend: the shared, multi-user store for the engineering graph.

The local SQLite backend keeps a single engineer's history on disk. This backend puts the same two
tables in Appwrite TablesDB so that impact analysis and device lineage are queryable by a whole
team, and across machines, without moving the design files themselves off the engineer's disk.

Design notes:

* **Row ids are deterministic.** Appwrite row ids are limited to 36 characters of ``[A-Za-z0-9._-]``,
  while graph node ids look like ``component:U1`` or ``net:+3V3``. Each row id is therefore a hash of
  the revision plus the node's natural key, which also makes a rebuild idempotent: re-importing a
  revision upserts the same rows rather than duplicating them.
* **Writes are buffered and sent in bulk.** A sample board is a few hundred rows; one HTTP request per
  row would make a rebuild unusable, so writes queue until :meth:`flush` and go out via ``upsert_rows``.
* **Reads work two ways.** With ``prefetch`` (the default) a revision's subgraph is paged into memory
  once and traversed locally, which is what impact analysis needs. With ``prefetch=False`` every read
  is a live indexed query against TablesDB.
"""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from pathlib import Path

from ..schema import EDGE_COLUMNS, EDGES, NODE_COLUMNS, NODES
from .base import GraphBackend

BATCH = 100       # rows per bulk write
PAGE = 100        # rows per read page


class AppwriteConfigError(RuntimeError):
    """Appwrite is selected but not usable (missing SDK or incomplete configuration)."""


def find_dotenv(name: str = ".env", start: str | Path | None = None) -> Path | None:
    """Look for ``name`` in ``start`` and every parent directory, nearest first.

    ``vcm`` is normally run from inside a project folder while the credentials live at the top of
    the checkout, so a plain relative lookup would miss them.
    """
    here = Path(start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Read ``KEY=value`` lines into the environment without overriding what is already set.

    Credentials belong in a gitignored ``.env``, not on the command line where they reach shell
    history. A bare filename is searched for up the directory tree. Kept dependency-free on
    purpose; ``export`` prefixes and quotes are tolerated.
    """
    f = Path(path)
    loaded: dict[str, str] = {}
    if not f.is_file() and f.parent == Path("."):
        f = find_dotenv(f.name) or f
    if not f.is_file():
        return loaded
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            loaded[key] = value
            os.environ.setdefault(key, value)
    return loaded


def _as_dict(obj) -> dict:
    """Flatten one SDK model to a plain column dict.

    ``list_rows`` returns ``Row`` models whose user columns live under ``.data``, while the
    in-process fake returns plain dicts. Both shapes are normalised here so the backend never has
    to care which it is talking to.
    """
    if isinstance(obj, dict):
        return obj
    data = getattr(obj, "data", None)
    out = dict(data) if isinstance(data, dict) else {}
    rid = getattr(obj, "id", None)
    if rid is not None:
        out.setdefault("$id", rid)
    if not out and hasattr(obj, "to_dict"):
        out = obj.to_dict()
    return out


def _rows(page) -> list[dict]:
    """Rows of a list response, as plain dicts."""
    raw = page["rows"] if isinstance(page, dict) else getattr(page, "rows", [])
    return [_as_dict(r) for r in raw]


def _row_id(prefix: str, *parts: str) -> str:
    """Deterministic, Appwrite-safe row id (<=36 chars) for a natural key."""
    return prefix + hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:34]


class AppwriteConfig:
    """Connection settings, read from explicit arguments or the environment."""

    ENV = {"endpoint": "VCM_APPWRITE_ENDPOINT", "project": "VCM_APPWRITE_PROJECT",
           "api_key": "VCM_APPWRITE_API_KEY", "database": "VCM_APPWRITE_DATABASE"}

    def __init__(self, endpoint: str = "", project: str = "", api_key: str = "",
                 database: str = "", dotenv: str | Path | None = ".env"):
        if dotenv:
            load_dotenv(dotenv)
        self.endpoint = endpoint or os.environ.get(self.ENV["endpoint"], "")
        self.project = project or os.environ.get(self.ENV["project"], "")
        self.api_key = api_key or os.environ.get(self.ENV["api_key"], "")
        self.database = database or os.environ.get(self.ENV["database"], "vcm")

    @property
    def complete(self) -> bool:
        return bool(self.endpoint and self.project and self.api_key and self.database)

    def missing(self) -> list[str]:
        return [env for k, env in self.ENV.items() if not getattr(self, k)]

    def require(self) -> None:
        if not self.complete:
            raise AppwriteConfigError(
                "Appwrite backend selected but not configured. Set: " + ", ".join(self.missing()))


def tables_db(cfg: AppwriteConfig):
    """Build an Appwrite ``TablesDB`` service client, with a readable error when unavailable."""
    cfg.require()
    try:
        from appwrite.client import Client
        from appwrite.services.tables_db import TablesDB
    except ImportError as exc:  # pragma: no cover - exercised only without the optional dep
        raise AppwriteConfigError(
            "the 'appwrite' package is required for the Appwrite backend: pip install -e '.[appwrite]'"
        ) from exc
    client = Client()
    client.set_endpoint(cfg.endpoint).set_project(cfg.project).set_key(cfg.api_key)
    return TablesDB(client)


class AppwriteBackend(GraphBackend):
    """Graph storage on Appwrite TablesDB.

    ``service`` may be injected for testing; otherwise it is built from ``cfg``.
    """

    name = "appwrite"

    def __init__(self, cfg: AppwriteConfig | None = None, service=None, prefetch: bool = True):
        self.cfg = cfg or AppwriteConfig()
        self.db = self.cfg.database
        self.svc = service if service is not None else tables_db(self.cfg)
        self.prefetch = prefetch
        self._pending: dict[str, dict[str, dict]] = {NODES: {}, EDGES: {}}
        self._cache: dict[str, dict] = {}

    # ---- queries ---------------------------------------------------
    @staticmethod
    def _q():
        from appwrite.query import Query
        return Query

    def _list_all(self, table: str, queries: list[str]) -> list[dict]:
        """Page through every row matching ``queries``."""
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

    # ---- cache -----------------------------------------------------
    def _hydrate(self, rev: str) -> dict:
        """Page a revision's whole subgraph into memory once, indexed for traversal."""
        if rev in self._cache:
            return self._cache[rev]
        Query = self._q()
        nodes = {r["node_id"]: r for r in self._list_all(NODES, [Query.equal("rev", rev)])}
        out: dict[tuple[str, str], list] = defaultdict(list)
        into: dict[tuple[str, str], list] = defaultdict(list)
        edge_counts: dict[str, int] = defaultdict(int)
        for r in self._list_all(EDGES, [Query.equal("rev", rev)]):
            props = r.get("props") or "{}"
            for key, bucket, other in (("src", out, "dst"), ("dst", into, "src")):
                bucket[(r[key], r["type"])].append((r[other], props))
                bucket[(r[key], "")].append((r[other], props))
            edge_counts[r["type"]] += 1
        cache = {"nodes": nodes, "out": out, "into": into, "edge_counts": dict(edge_counts)}
        self._cache[rev] = cache
        return cache

    def _invalidate(self, rev: str) -> None:
        self._cache.pop(rev, None)

    # ---- writes ----------------------------------------------------
    def clear(self, rev: str) -> None:
        self.flush()
        Query = self._q()
        for table in (NODES, EDGES):
            try:
                self.svc.delete_rows(self.db, table, queries=[Query.equal("rev", rev)])
            except Exception as exc:  # table may not exist yet on a first run
                if "not found" not in str(exc).lower():
                    raise
        self._invalidate(rev)

    def put_node(self, rev: str, node_id: str, type_: str, name: str, props: str) -> None:
        self._queue(NODES, {"$id": _row_id("n", rev, node_id), "rev": rev, "node_id": node_id,
                            "type": type_, "name": name[:512], "props": props})
        self._invalidate(rev)

    def put_edge(self, rev: str, src: str, dst: str, type_: str, props: str) -> None:
        self._queue(EDGES, {"$id": _row_id("e", rev, src, dst, type_, props), "rev": rev,
                            "src": src, "dst": dst, "type": type_, "props": props})
        self._invalidate(rev)

    def _queue(self, table: str, row: dict) -> None:
        # Keyed by row id: a node may legitimately be written more than once while a revision is
        # built (a net is re-added for every pad on it), and a bulk upsert rejects a batch that
        # repeats an id. Last write wins, matching SQLite's INSERT OR REPLACE.
        self._pending[table][row["$id"]] = row
        if len(self._pending[table]) >= BATCH:
            self._send(table)

    def _send(self, table: str) -> None:
        rows, self._pending[table] = list(self._pending[table].values()), {}
        for i in range(0, len(rows), BATCH):
            self.svc.upsert_rows(self.db, table, rows=rows[i:i + BATCH])

    def flush(self) -> None:
        for table in (NODES, EDGES):
            if self._pending[table]:
                self._send(table)

    # ---- reads -----------------------------------------------------
    def get_node(self, rev: str, node_id: str) -> tuple[str, str, str, str] | None:
        self.flush()
        if self.prefetch:
            r = self._hydrate(rev)["nodes"].get(node_id)
        else:
            Query = self._q()
            rows = self._list_all(NODES, [Query.equal("rev", rev), Query.equal("node_id", node_id)])
            r = rows[0] if rows else None
        return (r["node_id"], r["type"], r["name"], r.get("props") or "{}") if r else None

    def node_ids(self, rev: str, type_: str | None = None) -> list[str]:
        self.flush()
        if self.prefetch:
            nodes = self._hydrate(rev)["nodes"].values()
            return sorted(r["node_id"] for r in nodes if not type_ or r["type"] == type_)
        Query = self._q()
        q = [Query.equal("rev", rev)] + ([Query.equal("type", type_)] if type_ else [])
        return sorted(r["node_id"] for r in self._list_all(NODES, q))

    def out_edges(self, rev: str, src: str, type_: str | None = None) -> list[tuple[str, str]]:
        return self._edges(rev, "src", "dst", src, type_)

    def in_edges(self, rev: str, dst: str, type_: str | None = None) -> list[tuple[str, str]]:
        return self._edges(rev, "dst", "src", dst, type_)

    def _edges(self, rev: str, key: str, other: str, value: str,
               type_: str | None) -> list[tuple[str, str]]:
        self.flush()
        if self.prefetch:
            bucket = self._hydrate(rev)["out" if key == "src" else "into"]
            return sorted(bucket.get((value, type_ or ""), []))
        Query = self._q()
        q = [Query.equal("rev", rev), Query.equal(key, value)]
        if type_:
            q.append(Query.equal("type", type_))
        return sorted((r[other], r.get("props") or "{}") for r in self._list_all(EDGES, q))

    def counts(self, rev: str) -> tuple[dict[str, int], dict[str, int]]:
        self.flush()
        cache = self._hydrate(rev) if self.prefetch else None
        if cache is not None:
            nodes: dict[str, int] = defaultdict(int)
            for r in cache["nodes"].values():
                nodes[r["type"]] += 1
            return dict(sorted(nodes.items())), dict(sorted(cache["edge_counts"].items()))
        return self._server_counts(NODES, rev), self._server_counts(EDGES, rev)

    def _server_counts(self, table: str, rev: str) -> dict[str, int]:
        """Aggregate by type in TablesDB rather than pulling rows back."""
        Query = self._q()
        page = self.svc.list_rows(self.db, table, queries=[
            Query.equal("rev", rev), Query.group_by(["type"]), Query.count("n"), Query.limit(PAGE)])
        rows = _rows(page)
        return {r["type"]: int(r.get("n", 0)) for r in sorted(rows, key=lambda r: r["type"])}

    def close(self) -> None:
        self.flush()


# ---- provisioning --------------------------------------------------
_CREATORS = {"varchar": "create_varchar_column", "text": "create_text_column",
             "mediumtext": "create_mediumtext_column"}


def error_code(exc: Exception) -> int:
    """HTTP-style status of an SDK error (``AppwriteException.code``), or 0 if it has none."""
    code = getattr(exc, "code", 0)
    return code if isinstance(code, int) else 0


def public_events_requested() -> bool:
    """Opt-in: make the (payload-free) event feed world-readable so browsers can use Realtime."""
    return os.environ.get("VCM_APPWRITE_REALTIME", "").lower() in ("1", "true", "yes")


def provision(cfg: AppwriteConfig | None = None, service=None, public_events: bool | None = None
              ) -> dict:
    """Create the database, every table (graph, records, events), columns and indexes. Idempotent.

    ``public_events`` (default: ``$VCM_APPWRITE_REALTIME``) grants the world read access to the
    events table only, which carries no record content. Everything else stays private.
    """
    cfg = cfg or AppwriteConfig()
    svc = service if service is not None else tables_db(cfg)
    from appwrite.enums.tables_db_index_type import TablesDBIndexType
    from appwrite.permission import Permission
    from appwrite.role import Role

    from ..schema import (
        EDGE_INDEXES,
        EVENT_COLUMNS,
        EVENT_INDEXES,
        EVENTS,
        NODE_INDEXES,
        RECORD_COLUMNS,
        RECORD_INDEXES,
        RECORDS,
    )
    public = public_events_requested() if public_events is None else public_events
    done: dict[str, list[str]] = {"created": [], "existed": []}

    def step(label: str, fn) -> None:
        try:
            fn()
            done["created"].append(label)
        except Exception as exc:
            if error_code(exc) == 409 or "already exists" in str(exc).lower() or "409" in str(exc):
                done["existed"].append(label)
            else:
                raise

    step(f"database:{cfg.database}", lambda: svc.create(cfg.database, "VCM engineering graph"))
    for table, columns, indexes in ((NODES, NODE_COLUMNS, NODE_INDEXES),
                                    (EDGES, EDGE_COLUMNS, EDGE_INDEXES),
                                    (RECORDS, RECORD_COLUMNS, RECORD_INDEXES),
                                    (EVENTS, EVENT_COLUMNS, EVENT_INDEXES)):
        perms = [Permission.read(Role.any())] if (table == EVENTS and public) else None
        step(f"table:{table}", lambda t=table, p=perms: svc.create_table(
            cfg.database, t, t, permissions=p))
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
    """Report whether the configured Appwrite database is reachable and provisioned."""
    cfg = cfg or AppwriteConfig()
    if not cfg.complete:
        return {"ok": False, "configured": False, "missing": cfg.missing()}
    try:
        svc = service if service is not None else tables_db(cfg)
        listing = svc.list_tables(cfg.database)
        raw = listing["tables"] if isinstance(listing, dict) else getattr(listing, "tables", [])
        names = {getattr(t, "id", None) or _as_dict(t).get("$id") for t in raw}
        from ..schema import EVENTS, RECORDS
        missing = [t for t in (NODES, EDGES) if t not in names]
        no_records = [t for t in (RECORDS, EVENTS) if t not in names]
        return {"ok": not missing, "configured": True, "endpoint": cfg.endpoint,
                "database": cfg.database, "tables": sorted(names), "missing_tables": missing,
                "records_ready": not no_records, "missing_record_tables": no_records}
    except Exception as exc:
        return {"ok": False, "configured": True, "endpoint": cfg.endpoint, "error": str(exc)}


__all__ = ["AppwriteBackend", "AppwriteConfig", "AppwriteConfigError", "provision", "health",
           "load_dotenv", "find_dotenv", "error_code", "public_events_requested"]
