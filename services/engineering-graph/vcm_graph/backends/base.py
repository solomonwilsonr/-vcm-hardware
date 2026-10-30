"""Storage interface for the engineering graph.

Every backend stores the same two record shapes and nothing else; all graph semantics
(net traversal, impact scoping) live in :class:`vcm_graph.graph.EngineeringGraph` and work
unchanged against any backend.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class GraphBackend(ABC):
    """Two tables scoped by revision. Writes may be buffered until :meth:`flush`."""

    #: Human-readable backend name, surfaced by ``vcm graph stats`` and the API.
    name = "base"

    @abstractmethod
    def clear(self, rev: str) -> None:
        """Drop every node and edge belonging to ``rev``."""

    @abstractmethod
    def put_node(self, rev: str, node_id: str, type_: str, name: str, props: str) -> None:
        """Insert or replace a node. ``props`` is a canonical JSON string."""

    @abstractmethod
    def put_edge(self, rev: str, src: str, dst: str, type_: str, props: str) -> None:
        """Append an edge. ``props`` is a canonical JSON string."""

    @abstractmethod
    def get_node(self, rev: str, node_id: str) -> tuple[str, str, str, str] | None:
        """``(node_id, type, name, props)`` or None."""

    @abstractmethod
    def node_ids(self, rev: str, type_: str | None = None) -> list[str]:
        """Sorted node ids, optionally filtered by type."""

    @abstractmethod
    def out_edges(self, rev: str, src: str, type_: str | None = None) -> list[tuple[str, str]]:
        """``(dst, props)`` pairs sorted by dst."""

    @abstractmethod
    def in_edges(self, rev: str, dst: str, type_: str | None = None) -> list[tuple[str, str]]:
        """``(src, props)`` pairs sorted by src."""

    @abstractmethod
    def counts(self, rev: str) -> tuple[dict[str, int], dict[str, int]]:
        """``(nodes_by_type, edges_by_type)`` for ``rev``."""

    def flush(self) -> None:
        """Make buffered writes durable. Safe to call repeatedly; a no-op when unbuffered."""
        return None

    def close(self) -> None:
        """Release resources. Safe to call repeatedly."""
        return None
