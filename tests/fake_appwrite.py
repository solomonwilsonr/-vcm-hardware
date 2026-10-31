"""An in-process stand-in for Appwrite TablesDB.

It speaks the same method names and the same query wire format as the real SDK service, so the
Appwrite backend can be exercised fully offline (a project rule: tests must pass without network).
It also counts requests, which is how the batching behaviour is asserted.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict


class AppwriteError(Exception):
    pass


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
    plan: dict = {"equal": {}, "limit": None, "offset": 0, "group_by": [], "count": None}
    for raw in queries or []:
        q = json.loads(raw)
        method, values, attr = q.get("method"), q.get("values", []), q.get("attribute")
        if method == "equal":
            plan["equal"][attr] = values[0]
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
        self.requests: list[tuple[str, str, int]] = []   # (op, table, row count)

    # ---- schema ----------------------------------------------------
    def create(self, database_id, name, **kw):
        if database_id in self.databases:
            raise AppwriteError(f"Database already exists: {database_id}")
        self.databases[database_id] = name
        return {"$id": database_id, "name": name}

    def create_table(self, database_id, table_id, name, **kw):
        if table_id in self.tables:
            raise AppwriteError(f"Table already exists: {table_id}")
        self.tables[table_id] = {}
        return {"$id": table_id, "name": name}

    def list_tables(self, database_id, **kw):
        if database_id not in self.databases:
            raise AppwriteError(f"Database not found: {database_id}")
        if self.models:
            return _Listing("tables", [_Table(t) for t in sorted(self.tables)])
        return {"tables": [{"$id": t} for t in sorted(self.tables)], "total": len(self.tables)}

    def _add_column(self, table_id, key, spec):
        if key in self.columns[table_id]:
            raise AppwriteError(f"Column already exists: {key}")
        self.columns[table_id][key] = spec
        return {"key": key}

    def create_varchar_column(self, database_id, table_id, key, size, required, **kw):
        return self._add_column(table_id, key, ("varchar", size, required))

    def create_text_column(self, database_id, table_id, key, required, **kw):
        return self._add_column(table_id, key, ("text", 0, required))

    def create_index(self, database_id, table_id, key, type, columns, lengths=None, **kw):
        if key in self.indexes[table_id]:
            raise AppwriteError(f"Index already exists: {key}")
        self.indexes[table_id][key] = (tuple(columns), tuple(lengths or ()))
        return {"key": key}

    # ---- rows ------------------------------------------------------
    def _table(self, table_id) -> dict[str, dict]:
        if table_id not in self.tables:
            raise AppwriteError(f"Table with the requested ID could not be found: {table_id}")
        return self.tables[table_id]

    def upsert_rows(self, database_id, table_id, rows, **kw):
        t = self._table(table_id)
        self.requests.append(("upsert", table_id, len(rows)))
        seen = set()
        for row in rows:
            rid = row["$id"]
            if rid in seen:      # the real service rejects a batch that repeats an id
                raise AppwriteError(f"Row with the requested ID '{rid}' already exists.")
            seen.add(rid)
            t[rid] = dict(row)
        return {"rows": list(rows), "total": len(rows)}

    def _match(self, table_id, plan) -> list[dict]:
        return [r for r in self._table(table_id).values()
                if all(r.get(k) == v for k, v in plan["equal"].items())]

    def list_rows(self, database_id, table_id, queries=None, **kw):
        plan = _parse(queries)
        self.requests.append(("list", table_id, 0))
        rows = sorted(self._match(table_id, plan), key=lambda r: r["$id"])
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


# ---- VectorsDB -----------------------------------------------------
VEC_DIM = 64


def _tokens(text: str) -> list[str]:
    out, cur = [], ""
    for ch in text.lower():
        if ch.isalnum():
            cur += ch
        elif cur:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def fake_embed(text: str, dim: int = VEC_DIM) -> list[float]:
    """A deterministic bag-of-tokens vector, L2 normalised.

    Not a language model, but it has the one property the tests need: texts that share vocabulary
    land near each other under cosine similarity, so "300 W dual-slot accelerator" really does rank
    a GPU above a NIC. That makes the search assertions meaningful rather than circular.
    """
    vec = [0.0] * dim
    for tok in _tokens(text):
        h = int(hashlib.sha256(tok.encode()).hexdigest()[:8], 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeEmbeddings:
    """Stands in for ``appwrite.services.embeddings.Embeddings``."""

    def __init__(self, dim: int = VEC_DIM, models: bool = False):
        self.dim, self.models, self.calls = dim, models, []

    def create_text_embeddings(self, texts, model=None):
        self.calls.append((list(texts), model))
        items = [{"model": model or "fake", "dimension": self.dim,
                  "embedding": fake_embed(t, self.dim), "error": ""} for t in texts]
        if self.models:
            return _Listing("embeddings", [_Embedding(i) for i in items])
        return {"embeddings": items, "total": len(items)}


class _Embedding:
    def __init__(self, raw: dict):
        self.model, self.dimension = raw["model"], raw["dimension"]
        self.embedding, self.error = raw["embedding"], raw["error"]


class FakeVectorsDB:
    """Minimal VectorsDB: one collection of documents, cosine nearest-neighbour search."""

    def __init__(self, models: bool = False):
        self.models = models
        self.databases: dict[str, str] = {}
        self.collections: dict[str, dict] = {}
        self.documents: dict[str, dict[str, dict]] = defaultdict(dict)
        self.indexes: dict[str, dict[str, tuple]] = defaultdict(dict)

    def create(self, database_id, name, **kw):
        if database_id in self.databases:
            raise AppwriteError(f"Database already exists: {database_id}")
        self.databases[database_id] = name
        return {"$id": database_id}

    def create_collection(self, database_id, collection_id, name, dimension, **kw):
        if collection_id in self.collections:
            raise AppwriteError(f"Collection already exists: {collection_id}")
        self.collections[collection_id] = {"name": name, "dimension": dimension}
        return {"$id": collection_id, "dimension": dimension}

    def create_index(self, database_id, collection_id, key, type, attributes, **kw):
        if key in self.indexes[collection_id]:
            raise AppwriteError(f"Index already exists: {key}")
        self.indexes[collection_id][key] = (getattr(type, "value", type), tuple(attributes))
        return {"key": key}

    def _collection(self, collection_id) -> dict[str, dict]:
        if collection_id not in self.collections:
            raise AppwriteError(f"Collection not found: {collection_id}")
        return self.documents[collection_id]

    def upsert_documents(self, database_id, collection_id, documents, **kw):
        col = self._collection(collection_id)
        dim = self.collections[collection_id]["dimension"]
        for doc in documents:
            vec = doc.get("embedding") or []
            if len(vec) != dim:
                raise AppwriteError(
                    f"Invalid document: expected {dim} dimensions, got {len(vec)}")
            col[doc["$id"]] = dict(doc)
        return {"documents": list(documents), "total": len(documents)}

    def create_query(self, database_id, collection_id, queries=None, **kw):
        col = self._collection(collection_id)
        plan = _parse(queries)
        vector, rows = None, list(col.values())
        for raw in queries or []:
            q = json.loads(raw)
            if q.get("method") == "vectorCosine":
                vector = q["values"][0]
        rows = [r for r in rows if all(r.get(k) == v for k, v in plan["equal"].items())]
        if vector is not None:
            rows.sort(key=lambda r: -_cosine(vector, r.get("embedding") or []))
        rows = rows[:plan["limit"]] if plan["limit"] is not None else rows
        if self.models:
            return _Listing("documents", [_Row(r) for r in rows])
        return {"documents": rows, "total": len(rows)}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
