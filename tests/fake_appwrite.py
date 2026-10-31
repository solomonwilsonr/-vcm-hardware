"""An in-process stand-in for Appwrite TablesDB.

It speaks the same method names and the same query wire format as the real SDK service, so the
Appwrite backend can be exercised fully offline (a project rule: tests must pass without network).
It also counts requests, which is how the batching behaviour is asserted.
"""
from __future__ import annotations

import json
import threading
from collections import defaultdict


class AppwriteError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


class _Row:
    """Mimics ``appwrite.models.row.Row``: internal fields as attributes, columns under ``.data``."""

    INTERNAL = {"$id", "$sequence", "$tableId", "$databaseId", "$createdAt", "$updatedAt",
                "$permissions"}

    def __init__(self, raw: dict):
        self.id = raw.get("$id", "")
        self.data = {k: v for k, v in raw.items() if k not in self.INTERNAL}


class _Listing:
    """Mimics a ``RowList``/``TableList`` model."""

    def __init__(self, key: str, items: list):
        setattr(self, key, items)
        self.total = len(items)


class _Table:
    def __init__(self, table_id: str):
        self.id = table_id


def _parse(queries: list[str] | None) -> dict:
    """Decode SDK query strings into a simple plan."""
    plan: dict = {"equal": {}, "gt": {}, "order": None, "limit": None, "offset": 0, "group_by": [],
                  "count": None}
    for raw in queries or []:
        q = json.loads(raw)
        method, values, attr = q.get("method"), q.get("values", []), q.get("attribute")
        if method == "equal":
            plan["equal"][attr] = values[0]
        elif method == "greaterThan":
            plan["gt"][attr] = values[0]
        elif method == "orderAsc":
            plan["order"] = attr
        elif method == "limit":
            plan["limit"] = values[0]
        elif method == "offset":
            plan["offset"] = values[0]
        elif method == "groupBy":
            plan["group_by"] = list(values)
        elif method == "count":
            plan["count"] = attr
    return plan


class FakeTablesDB:
    """Minimal TablesDB: tables of rows keyed by ``$id``, with equality queries and paging."""

    def __init__(self, models: bool = False):
        #: when True, return SDK-shaped model objects instead of plain dicts
        self.models = models
        self.databases: dict[str, str] = {}
        self.tables: dict[str, dict[str, dict]] = {}
        self.columns: dict[str, dict[str, tuple]] = defaultdict(dict)
        self.indexes: dict[str, dict[str, tuple]] = defaultdict(dict)
        self._lock = threading.Lock()   # the real service creates a row atomically
        self.table_permissions: dict[str, list | None] = {}
        self.requests: list[tuple[str, str, int]] = []   # (op, table, row count)

    # ---- schema ----------------------------------------------------
    def create(self, database_id, name, **kw):
        if database_id in self.databases:
            raise AppwriteError(f"Database already exists: {database_id}", 409)
        self.databases[database_id] = name
        return {"$id": database_id, "name": name}

    def create_table(self, database_id, table_id, name, **kw):
        if table_id in self.tables:
            raise AppwriteError(f"Table already exists: {table_id}", 409)
        self.tables[table_id] = {}
        self.table_permissions[table_id] = kw.get("permissions")
        return {"$id": table_id, "name": name}

    def list_tables(self, database_id, **kw):
        if self.models:
            return _Listing("tables", [_Table(t) for t in sorted(self.tables)])
        return {"tables": [{"$id": t} for t in sorted(self.tables)], "total": len(self.tables)}

    def _add_column(self, table_id, key, spec):
        if key in self.columns[table_id]:
            raise AppwriteError(f"Column already exists: {key}", 409)
        self.columns[table_id][key] = spec
        return {"key": key}

    def create_varchar_column(self, database_id, table_id, key, size, required, **kw):
        return self._add_column(table_id, key, ("varchar", size, required))

    def create_text_column(self, database_id, table_id, key, required, **kw):
        return self._add_column(table_id, key, ("text", 0, required))

    def create_mediumtext_column(self, database_id, table_id, key, required, **kw):
        return self._add_column(table_id, key, ("mediumtext", 0, required))

    def create_index(self, database_id, table_id, key, type, columns, lengths=None, **kw):
        if key in self.indexes[table_id]:
            raise AppwriteError(f"Index already exists: {key}", 409)
        self.indexes[table_id][key] = (tuple(columns), tuple(lengths or ()))
        return {"key": key}

    # ---- rows ------------------------------------------------------
    def _table(self, table_id) -> dict[str, dict]:
        if table_id not in self.tables:
            raise AppwriteError(f"Table with the requested ID could not be found: {table_id}", 404)
        return self.tables[table_id]

    def upsert_rows(self, database_id, table_id, rows, **kw):
        t = self._table(table_id)
        self.requests.append(("upsert", table_id, len(rows)))
        seen = set()
        for row in rows:
            rid = row["$id"]
            if rid in seen:      # the real service rejects a batch that repeats an id
                raise AppwriteError(f"Row with the requested ID '{rid}' already exists.", 409)
            seen.add(rid)
            t[rid] = dict(row)
        return {"rows": list(rows), "total": len(rows)}

    def create_row(self, database_id, table_id, row_id, data, permissions=None, **kw):
        t = self._table(table_id)
        with self._lock:
            self.requests.append(("create", table_id, 1))
            if row_id in t:
                raise AppwriteError(f"Row with the requested ID '{row_id}' already exists.", 409)
            t[row_id] = {**data, "$id": row_id}
        return self._one(t[row_id])

    def get_row(self, database_id, table_id, row_id, queries=None, **kw):
        t = self._table(table_id)
        self.requests.append(("get", table_id, 1))
        if row_id not in t:
            raise AppwriteError(f"Row with the requested ID could not be found: {row_id}", 404)
        return self._one(t[row_id])

    def update_row(self, database_id, table_id, row_id, data=None, permissions=None, **kw):
        t = self._table(table_id)
        self.requests.append(("update", table_id, 1))
        if row_id not in t:
            raise AppwriteError(f"Row with the requested ID could not be found: {row_id}", 404)
        t[row_id].update(data or {})
        return self._one(t[row_id])

    def delete_row(self, database_id, table_id, row_id, **kw):
        t = self._table(table_id)
        self.requests.append(("delete", table_id, 1))
        if row_id not in t:
            raise AppwriteError(f"Row with the requested ID could not be found: {row_id}", 404)
        del t[row_id]
        return {}

    def _one(self, raw: dict):
        return _Row(raw) if self.models else dict(raw)

    def _match(self, table_id, plan) -> list[dict]:
        return [r for r in self._table(table_id).values()
                if all(r.get(k) == v for k, v in plan["equal"].items())
                and all(str(r.get(k, "")) > v for k, v in plan["gt"].items())]

    def list_rows(self, database_id, table_id, queries=None, **kw):
        plan = _parse(queries)
        self.requests.append(("list", table_id, 0))
        order = plan["order"] or "$id"
        rows = sorted(self._match(table_id, plan), key=lambda r: str(r.get(order, "")))
        if plan["group_by"]:  # aggregates come back as rows of group keys plus the count
            groups: dict[tuple, int] = defaultdict(int)
            for r in rows:
                groups[tuple(r.get(k) for k in plan["group_by"])] += 1
            out = []
            for key, n in groups.items():
                row = dict(zip(plan["group_by"], key, strict=True))
                row[plan["count"] or "count"] = n
                out.append(row)
            return self._page(out)
        start = plan["offset"]
        end = start + plan["limit"] if plan["limit"] is not None else None
        return self._page(rows[start:end], total=len(rows))

    def _page(self, rows: list[dict], total: int | None = None):
        if self.models:
            return _Listing("rows", [_Row(r) for r in rows])
        return {"rows": rows, "total": total if total is not None else len(rows)}

    def delete_rows(self, database_id, table_id, queries=None, **kw):
        plan = _parse(queries)
        t = self._table(table_id)
        self.requests.append(("delete", table_id, 0))
        doomed = [r["$id"] for r in self._match(table_id, plan)]
        for rid in doomed:
            del t[rid]
        return {"rows": [], "total": len(doomed)}

    # ---- helpers for assertions ------------------------------------
    def row_count(self, table_id) -> int:
        return len(self.tables.get(table_id, {}))

    def ops(self, op: str) -> list[tuple[str, str, int]]:
        return [r for r in self.requests if r[0] == op]

    def reset_requests(self) -> None:
        self.requests.clear()
