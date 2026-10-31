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

# ---- shared records: releases, devices, pull requests, locks (see vcm_core.records) -------------
RECORDS = "vcm_records"
EVENTS = "vcm_events"

RECORD_COLUMNS: dict[str, tuple[str, int, bool]] = {
    "scope": ("varchar", 64, True),     # one team/project per scope, so projects can share a database
    "kind": ("varchar", 32, True),
    "name": ("varchar", 256, True),
    "body": ("mediumtext", 0, True),    # a PR carries its diff and impact report: can exceed 64 kB
    "updated_at": ("varchar", 32, True),
}

# A change feed with no payload: what changed, never the content. It is the one table that may be
# made publicly readable (opt-in) so a browser can subscribe to Appwrite Realtime without a key.
EVENT_COLUMNS: dict[str, tuple[str, int, bool]] = {
    "scope": ("varchar", 64, True),
    "at": ("varchar", 32, True),
    "kind": ("varchar", 32, True),
    "name": ("varchar", 256, True),
    "action": ("varchar", 16, True),
    "actor": ("varchar", 128, False),
}

RECORD_INDEXES: dict[str, tuple[list[str], list[int]]] = {
    "rec_scope_kind": (["scope", "kind"], [64, 32]),
}

EVENT_INDEXES: dict[str, tuple[list[str], list[int]]] = {
    "evt_scope_at": (["scope", "at"], [64, 32]),
}

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(rev TEXT, id TEXT, type TEXT, name TEXT, props TEXT,
                                 PRIMARY KEY(rev, id));
CREATE TABLE IF NOT EXISTS edges(rev TEXT, src TEXT, dst TEXT, type TEXT, props TEXT);
CREATE INDEX IF NOT EXISTS edges_src ON edges(rev, src, type);
CREATE INDEX IF NOT EXISTS edges_dst ON edges(rev, dst, type);
"""
