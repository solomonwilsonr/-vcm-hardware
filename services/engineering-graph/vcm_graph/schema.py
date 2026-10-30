"""Shared table shape for every graph backend.

The graph is two tables, ``nodes`` and ``edges``, both scoped by ``rev`` so that revisions stay
isolated in one store. SQLite and Appwrite TablesDB implement the same shape; this module is the
single definition both read, so provisioning and queries cannot drift apart.
"""
from __future__ import annotations

NODES = "vcm_nodes"
EDGES = "vcm_edges"

# key -> (appwrite column type, size, required)
NODE_COLUMNS: dict[str, tuple[str, int, bool]] = {
    "rev": ("varchar", 64, True),
    "node_id": ("varchar", 512, True),
    "type": ("varchar", 32, True),
    "name": ("varchar", 512, True),
    "props": ("text", 0, False),
}

EDGE_COLUMNS: dict[str, tuple[str, int, bool]] = {
    "rev": ("varchar", 64, True),
    "src": ("varchar", 512, True),
    "dst": ("varchar", 512, True),
    "type": ("varchar", 32, True),
    "props": ("text", 0, False),
}

# name -> (columns, index key lengths). Lengths keep composite keys inside InnoDB's limit.
NODE_INDEXES: dict[str, tuple[list[str], list[int]]] = {
    "node_rev_id": (["rev", "node_id"], [64, 191]),
    "node_rev_type": (["rev", "type"], [64, 32]),
}

EDGE_INDEXES: dict[str, tuple[list[str], list[int]]] = {
    "edge_rev_src": (["rev", "src", "type"], [64, 191, 32]),
    "edge_rev_dst": (["rev", "dst", "type"], [64, 191, 32]),
}

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(rev TEXT, id TEXT, type TEXT, name TEXT, props TEXT,
                                 PRIMARY KEY(rev, id));
CREATE TABLE IF NOT EXISTS edges(rev TEXT, src TEXT, dst TEXT, type TEXT, props TEXT);
CREATE INDEX IF NOT EXISTS edges_src ON edges(rev, src, type);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(rev, dst, type);
"""
