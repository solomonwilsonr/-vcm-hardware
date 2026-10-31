"""Engineering dependency graph (pluggable storage: local SQLite or Appwrite TablesDB).

Importing this package also registers the ``appwrite`` records backend with ``vcm_core`` (lazily:
the Appwrite SDK is only imported if that backend is actually selected).
"""
from vcm_core import records as _records

from .backends import GraphBackend, SqliteBackend, open_backend
from .graph import EngineeringGraph, build_graph, project_config


def _appwrite_records(repo):
    import os

    from .backends.appwrite_records import AppwriteRecords
    return AppwriteRecords(scope=os.environ.get("VCM_APPWRITE_SCOPE")
                           or repo.config().get("appwrite_scope"))


_records.register_backend("appwrite", _appwrite_records)

__all__ = ["EngineeringGraph", "build_graph", "project_config", "GraphBackend", "SqliteBackend",
           "open_backend"]
