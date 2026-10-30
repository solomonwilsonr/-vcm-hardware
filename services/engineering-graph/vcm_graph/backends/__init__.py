"""Graph storage backends and the resolver that picks one."""
from __future__ import annotations

import os

from .base import GraphBackend
from .sqlite_backend import SqliteBackend

BACKEND_ENV = "VCM_GRAPH_BACKEND"


def open_backend(db_path: str = ":memory:", backend: str | None = None, **kwargs) -> GraphBackend:
    """Resolve a backend: explicit argument, then ``VCM_GRAPH_BACKEND``, then local SQLite.

    ``appwrite`` is imported lazily so the optional dependency is only needed when selected.
    """
    choice = (backend or os.environ.get(BACKEND_ENV) or "sqlite").lower()
    if choice == "sqlite":
        return SqliteBackend(db_path)
    if choice == "appwrite":
        from .appwrite_backend import AppwriteBackend
        return AppwriteBackend(**kwargs)
    raise ValueError(f"unknown graph backend: {choice!r} (expected 'sqlite' or 'appwrite')")


__all__ = ["GraphBackend", "SqliteBackend", "open_backend", "BACKEND_ENV"]
