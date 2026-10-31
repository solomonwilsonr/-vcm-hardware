"""Shared records on Appwrite TablesDB: releases, devices, pull requests, locks and a change feed.

This is what lets a team work against one set of review state and one set of locks instead of one
copy per laptop. Design notes:

* **Locks are atomic because row ids are unique.** A lock's row id is derived from its path, so
  ``create_row`` on a taken id fails with HTTP 409. That is the exclusive create the lock protocol
  needs, enforced by the database rather than by a check-then-write race in the client.
* **One scope per project.** Rows carry a ``scope`` (``$VCM_APPWRITE_SCOPE`` or ``appwrite_scope``
  in ``.vcm/config.json``, default ``default``), so several projects can share a database. A team
  that wants to see each other's records must use the same scope.
* **The change feed carries no content.** Each write appends ``{kind, name, action, actor, at}`` to
  the events table. It is the only table that can be made publicly readable (opt-in), which is what
  lets the dashboard subscribe to Appwrite Realtime straight from the browser without a key.
* The API key never leaves the server. Roles and identity are enforced by the VCM API in front of
  this store; row permissions are not a substitute for them.
"""
from __future__ import annotations

import json
import os

from vcm_core.records import make_event

from ..schema import EVENTS, RECORDS
from .appwrite_backend import AppwriteConfig, _as_dict, _row_id, _rows, error_code, tables_db

PAGE = 100


class AppwriteRecords:
    name = "appwrite"

    def __init__(self, cfg: AppwriteConfig | None = None, service=None, scope: str | None = None,
                 feed: bool = True):
        self.cfg = cfg or AppwriteConfig()
        self.db = self.cfg.database
        self.svc = service if service is not None else tables_db(self.cfg)
        self.scope = scope or os.environ.get("VCM_APPWRITE_SCOPE") or "default"
        self.feed = feed
        self.last_event_error: str | None = None

    @staticmethod
    def _q():
        from appwrite.query import Query
        return Query

    def _id(self, kind: str, name: str) -> str:
        return _row_id("r", self.scope, kind, name)

    def _row(self, kind: str, name: str, data: dict) -> dict:
        return {"scope": self.scope, "kind": kind, "name": name[:256], "body": json.dumps(data),
                "updated_at": make_event("", "", "")["at"]}

    def _emit(self, kind: str, name: str, action: str, data: dict | None) -> None:
        """Best effort: a lost notification must never fail the write it describes."""
        if not self.feed:
            return
        ev = make_event(kind, name, action, data)
        try:
            self.svc.create_row(self.db, EVENTS, ev["id"], {
                "scope": self.scope, "at": ev["at"], "kind": kind, "name": name[:256],
                "action": action, "actor": ev["actor"][:128]})
        except Exception as exc:
            self.last_event_error = str(exc)

    # ---- records ---------------------------------------------------
    def get(self, kind: str, name: str) -> dict | None:
        try:
            row = _as_dict(self.svc.get_row(self.db, RECORDS, self._id(kind, name)))
        except Exception as exc:
            if error_code(exc) == 404 or "could not be found" in str(exc).lower():
                return None
            raise
        return json.loads(row["body"])

    def put(self, kind: str, name: str, data: dict) -> None:
        rid, row = self._id(kind, name), self._row(kind, name, data)
        existed = self.get(kind, name) is not None
        self.svc.upsert_rows(self.db, RECORDS, rows=[{"$id": rid, **row}])
        self._emit(kind, name, "updated" if existed else "created", data)

    def create(self, kind: str, name: str, data: dict) -> bool:
        try:
            self.svc.create_row(self.db, RECORDS, self._id(kind, name), self._row(kind, name, data))
        except Exception as exc:
            if error_code(exc) == 409 or "already exists" in str(exc).lower():
                return False
            raise
        self._emit(kind, name, "created", data)
        return True

    def delete(self, kind: str, name: str) -> bool:
        try:
            self.svc.delete_row(self.db, RECORDS, self._id(kind, name))
        except Exception as exc:
            if error_code(exc) == 404 or "could not be found" in str(exc).lower():
                return False
            raise
        self._emit(kind, name, "deleted", None)
        return True

    def _all(self, kind: str) -> list[dict]:
        Query = self._q()
        out, offset = [], 0
        while True:
            page = _rows(self.svc.list_rows(self.db, RECORDS, queries=[
                Query.equal("scope", self.scope), Query.equal("kind", kind),
                Query.limit(PAGE), Query.offset(offset)]))
            out.extend(page)
            if len(page) < PAGE:
                return out
            offset += len(page)

    def names(self, kind: str) -> list[str]:
        return sorted(r["name"] for r in self._all(kind))

    def list(self, kind: str) -> list[dict]:
        rows = sorted(self._all(kind), key=lambda r: r["name"])
        return [json.loads(r["body"]) for r in rows]

    # ---- change feed -----------------------------------------------
    def events(self, after: str = "", limit: int = 200) -> list[dict]:
        Query = self._q()
        rows = _rows(self.svc.list_rows(self.db, EVENTS, queries=[
            Query.equal("scope", self.scope), Query.greater_than("at", after),
            Query.order_asc("at"), Query.limit(min(limit, PAGE))]))
        return [{"id": r.get("$id", ""), "at": r["at"], "kind": r["kind"], "name": r["name"],
                 "action": r["action"], "actor": r.get("actor", "")} for r in rows]


def realtime_config(cfg: AppwriteConfig | None = None, scope: str | None = None) -> dict:
    """Non-secret details a browser needs to subscribe to the events table, when the operator has
    opted in (``VCM_APPWRITE_REALTIME=1``, which also makes that table world-readable)."""
    from .appwrite_backend import public_events_requested
    cfg = cfg or AppwriteConfig()
    if not (public_events_requested() and cfg.endpoint and cfg.project):
        return {"enabled": False}
    return {"enabled": True, "endpoint": cfg.endpoint, "project": cfg.project,
            "scope": scope or os.environ.get("VCM_APPWRITE_SCOPE") or "default",
            "channels": [f"tablesdb.{cfg.database}.tables.{EVENTS}.rows",
                         f"databases.{cfg.database}.collections.{EVENTS}.documents"]}
