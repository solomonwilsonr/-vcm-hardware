"""Engineering dependency graph.

Nodes: component, net, footprint, artifact, firmware, test, device, release, revision.
Edges: connects_to, placed_on, configured_by, validated_by, built_from, defined_in, part_of.

Storage is pluggable (see :mod:`vcm_graph.backends`): local SQLite keeps a single engineer offline
and dependency-free, Appwrite TablesDB shares the same two tables across a team. Every graph
semantic below is backend-agnostic.
"""
from __future__ import annotations

import json
import re

from vcm_parsers import Design, design_from_revision

from .backends import GraphBackend, open_backend

GROUND_RE = re.compile(r"^(A|D|P|S)?GND\d*$|^VSS$|^0V$", re.I)


def global_nets(design) -> set[str]:
    """Nets every component sits on, which therefore carry no blast-radius information.

    Ground on a board and a PSU bank in a chassis are the same thing: shared by everything, so
    following them would report the whole design as affected by any change.
    """
    declared: set[str] = set()
    for node in (design.meta or {}).get("nodes", []):
        declared |= set((node.get("power") or {}).get("domains") or {})
    return declared

def project_config(repo, rev: str) -> dict:
    """Parsed ``vcm.json`` of a revision (firmware links, tests, rails); {} if absent."""
    r = repo.get_revision(rev)
    if "vcm.json" not in r.artifacts:
        return {}
    try:
        return json.loads(repo.read_artifact(r, "vcm.json"))
    except ValueError:
        return {}


class EngineeringGraph:
    """Revision-scoped view over a :class:`~vcm_graph.backends.base.GraphBackend`."""

    def __init__(self, db_path: str = ":memory:", rev: str = "",
                 backend: GraphBackend | str | None = None):
        self.backend = backend if isinstance(backend, GraphBackend) else open_backend(
            db_path, backend)
        self.rev = rev

    @property
    def backend_name(self) -> str:
        return self.backend.name

    # ---- writes ----------------------------------------------------
    def clear(self) -> None:
        self.backend.clear(self.rev)

    def add_node(self, node_id: str, type_: str, name: str, **props) -> None:
        self.backend.put_node(self.rev, node_id, type_, name, json.dumps(props, sort_keys=True))

    def add_edge(self, src: str, dst: str, type_: str, **props) -> None:
        self.backend.put_edge(self.rev, src, dst, type_, json.dumps(props, sort_keys=True))

    def flush(self) -> None:
        self.backend.flush()

    # ---- reads -----------------------------------------------------
    def node(self, node_id: str) -> dict | None:
        row = self.backend.get_node(self.rev, node_id)
        return {"id": row[0], "type": row[1], "name": row[2], **json.loads(row[3])} if row else None

    def nodes(self, type_: str | None = None) -> list[dict]:
        return [self.node(i) for i in self.backend.node_ids(self.rev, type_)]

    def out(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]:
        return [(d, json.loads(p)) for d, p in self.backend.out_edges(self.rev, node_id, edge_type)]

    def into(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]:
        return [(s, json.loads(p)) for s, p in self.backend.in_edges(self.rev, node_id, edge_type)]

    def stats(self) -> dict:
        n, e = self.backend.counts(self.rev)
        return {"nodes": n, "edges": e, "backend": self.backend.name}

    def neighbors_via_nets(self, comp_id: str) -> list[dict]:
        """Components sharing a non-ground net with comp_id."""
        found: dict[str, dict] = {}
        for net, _ in self.out(comp_id, "connects_to"):
            if (self.node(net) or {}).get("ground"):
                continue
            for other, _props in self.into(net, "connects_to"):
                if other != comp_id:
                    entry = found.setdefault(other, {"component": other, "via_nets": []})
                    entry["via_nets"].append(net.split(":", 1)[1])
        return [found[k] for k in sorted(found)]

    def affected_by(self, ref: str, depth: int = 1) -> dict:
        """Everything potentially affected by changing component ``ref`` (with graph paths as evidence)."""
        cid = f"component:{ref}"
        if not self.node(cid):
            raise KeyError(f"component {ref} not in graph for revision {self.rev[:10]}")
        seen = {cid}
        frontier = [cid]
        components: list[dict] = []
        for hop in range(1, depth + 1):
            nxt = []
            for c in frontier:
                for nb in self.neighbors_via_nets(c):
                    if nb["component"] in seen:
                        continue
                    seen.add(nb["component"])
                    nxt.append(nb["component"])
                    components.append({"ref": nb["component"].split(":", 1)[1], "hop": hop,
                                       "via_nets": sorted(set(nb["via_nets"])),
                                       "path": [c, f"net:{nb['via_nets'][0]}", nb["component"]]})
            frontier = nxt
        scope = sorted(seen)
        firmware, tests, footprints = {}, {}, {}
        for c in scope:
            for dst, _ in self.out(c, "configured_by"):
                firmware.setdefault(dst, []).append(c)
            for dst, _ in self.out(c, "validated_by"):
                tests.setdefault(dst, []).append(c)
            for dst, _ in self.out(c, "placed_on"):
                footprints.setdefault(dst, []).append(c)
        devices = sorted({d for rel, _ in self.into(f"revision:{self.rev}", "built_from")
                          for d, _ in self.into(rel, "built_from")})
        return {
            "component": ref, "revision": self.rev,
            "nets": [n.split(":", 1)[1] for n, _ in self.out(cid, "connects_to")],
            "components": components,
            "firmware": [{"node": k, "via": v} for k, v in sorted(firmware.items())],
            "tests": [{"node": k, "via": v} for k, v in sorted(tests.items())],
            "footprints": sorted(footprints),
            "devices": devices,
        }


def build_graph(repo, rev: str, design: Design | None = None, db_path: str | None = None,
                backend: GraphBackend | str | None = None) -> EngineeringGraph:
    """(Re)build the graph of ``rev``.

    Persisted to ``.vcm/graph.db`` by default; pass ``backend="appwrite"`` (or set
    ``VCM_GRAPH_BACKEND``) to build into the shared Appwrite TablesDB store instead.
    """
    revision = repo.get_revision(rev)
    design = design or design_from_revision(repo, revision)
    g = EngineeringGraph(db_path or str(repo.dir / "graph.db"), revision.id, backend)
    g.clear()
    rid = f"revision:{revision.id}"
    g.add_node(rid, "revision", revision.id[:10], message=revision.message)
    for path, art in revision.artifacts.items():
        g.add_node(f"artifact:{path}", "artifact", path, category=art.category, hash=art.hash)
    cfg = project_config(repo, revision.id)
    globals_ = global_nets(design)
    for ref, c in design.components.items():
        cid = f"component:{ref}"
        g.add_node(cid, "component", ref, value=c.value, footprint=c.footprint, mpn=c.mpn)
        g.add_edge(cid, rid, "part_of")
        if c.footprint:
            g.add_node(f"footprint:{c.footprint}", "footprint", c.footprint)
            g.add_edge(cid, f"footprint:{c.footprint}", "placed_on")
        for kind, paths in design.sources.items():
            if kind in c.by_source:
                for p in paths:
                    g.add_edge(cid, f"artifact:{p}", "defined_in", kind=kind)
        for pad, net in c.pads.items():
            if net:
                g.add_node(f"net:{net}", "net", net,
                           ground=bool(GROUND_RE.match(net)) or net in globals_)
                g.add_edge(cid, f"net:{net}", "connects_to", pad=pad)
    for link in cfg.get("firmware_links", []):
        for path in link.get("paths", []):
            g.add_node(f"firmware:{path}", "firmware", path)
            if path in revision.artifacts:
                g.add_edge(f"firmware:{path}", f"artifact:{path}", "defined_in")
            if link.get("component") in design.components:
                g.add_edge(f"component:{link['component']}", f"firmware:{path}", "configured_by")
    for t in cfg.get("tests", []):
        tid = f"test:{t['name']}"
        g.add_node(tid, "test", t["name"], evidence=t.get("evidence", ""))
        for ref in t.get("validates", []):
            if ref in design.components:
                g.add_edge(f"component:{ref}", tid, "validated_by")
    for rel in repo.list_records("releases"):
        g.add_node(f"release:{rel['name']}", "release", rel["name"])
        g.add_node(f"revision:{rel['revision']}", "revision", rel["revision"][:10])
        g.add_edge(f"release:{rel['name']}", f"revision:{rel['revision']}", "built_from")
    for dev in repo.list_records("devices"):
        g.add_node(f"device:{dev['serial']}", "device", dev["serial"], batch=dev.get("batch", ""))
        g.add_edge(f"device:{dev['serial']}", f"release:{dev['release']}", "built_from")
    g.flush()
    return g
