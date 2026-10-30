"""Local SQLite backend: the default, dependency-free, offline store."""
from __future__ import annotations

import sqlite3

from ..schema import SQLITE_SCHEMA
from .base import GraphBackend


class SqliteBackend(GraphBackend):
    name = "sqlite"

    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SQLITE_SCHEMA)

    def clear(self, rev: str) -> None:
        self.db.execute("DELETE FROM nodes WHERE rev=?", (rev,))
        self.db.execute("DELETE FROM edges WHERE rev=?", (rev,))

    def put_node(self, rev: str, node_id: str, type_: str, name: str, props: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO nodes VALUES(?,?,?,?,?)",
                        (rev, node_id, type_, name, props))

    def put_edge(self, rev: str, src: str, dst: str, type_: str, props: str) -> None:
        self.db.execute("INSERT INTO edges VALUES(?,?,?,?,?)", (rev, src, dst, type_, props))

    def get_node(self, rev: str, node_id: str) -> tuple[str, str, str, str] | None:
        return self.db.execute("SELECT id,type,name,props FROM nodes WHERE rev=? AND id=?",
                               (rev, node_id)).fetchone()

    def node_ids(self, rev: str, type_: str | None = None) -> list[str]:
        q, a = "SELECT id FROM nodes WHERE rev=?", [rev]
        if type_:
            q, a = q + " AND type=?", a + [type_]
        return [r[0] for r in self.db.execute(q + " ORDER BY id", a)]

    def out_edges(self, rev: str, src: str, type_: str | None = None) -> list[tuple[str, str]]:
        q, a = "SELECT dst,props FROM edges WHERE rev=? AND src=?", [rev, src]
        if type_:
            q, a = q + " AND type=?", a + [type_]
        return list(self.db.execute(q + " ORDER BY dst", a))

    def in_edges(self, rev: str, dst: str, type_: str | None = None) -> list[tuple[str, str]]:
        q, a = "SELECT src,props FROM edges WHERE rev=? AND dst=?", [rev, dst]
        if type_:
            q, a = q + " AND type=?", a + [type_]
        return list(self.db.execute(q + " ORDER BY src", a))

    def counts(self, rev: str) -> tuple[dict[str, int], dict[str, int]]:
        n = dict(self.db.execute("SELECT type,COUNT(*) FROM nodes WHERE rev=? GROUP BY type", (rev,)))
        e = dict(self.db.execute("SELECT type,COUNT(*) FROM edges WHERE rev=? GROUP BY type", (rev,)))
        return n, e

    def flush(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()
