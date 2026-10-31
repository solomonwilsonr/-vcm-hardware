"""Named JSON records, and where they live.

Most of what VCM stores is *derived*: the object store, snapshots and the dependency graph can all
be rebuilt from the design files in the repository, so keeping them on one engineer's disk costs
nothing. Two kinds of record are different. A **unit** (a physical node that was built and shipped)
and a **rack** (where units are installed) are facts about the world, not about the checkout. They
are created by whoever is standing in front of the hardware and read by everyone else.

Holding those in ``.vcm/devices/*.json`` makes every clone its own private fleet: a recall query
answers only about the units that this laptop happens to know about, which is exactly the question
it must never get wrong. So the record layer is pluggable. :class:`FileRecordStore` keeps the old
behaviour for everything derived and for working offline; the Appwrite-backed store in
``vcm_trace.fleet_store`` makes the fleet shared, with the local files demoted to a cache.

Kinds that stay local (``releases``, ``pull-requests``, ``proposals``) are repository-scoped and
travel with the checkout. Only ``devices`` and ``racks`` describe hardware that outlives it.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path


class RecordError(ValueError):
    """A record name that cannot be used as a file name or an Appwrite row id."""


#: record kinds that describe physical hardware, and so belong to the fleet rather than the clone
SHARED_KINDS = ("devices", "racks")


def check_name(kind: str, name: str) -> str:
    """Reject names that would escape the records directory or break a row id."""
    if not name or name.startswith(".") or any(c in name for c in "/\\\0"):
        raise RecordError(f"invalid {kind[:-1]} name: {name!r}")
    return name


class RecordStore(ABC):
    """Where named JSON records are kept.

    Implementations are responsible for durability only; validation, audit and the shape of the
    records themselves stay with the callers.
    """

    #: shown in diagnostics so it is always clear which store answered
    name = "abstract"

    @abstractmethod
    def save(self, kind: str, name: str, data: dict) -> Path:
        """Write ``data``, returning the local path it is mirrored to."""

    @abstractmethod
    def load(self, kind: str, name: str) -> dict | None:
        """Return the record, or ``None`` when it does not exist."""

    @abstractmethod
    def list(self, kind: str) -> list[dict]:
        """Every record of ``kind``, ordered by name."""

    @abstractmethod
    def has(self, kind: str, name: str) -> bool:
        """Whether the record exists, without paying to deserialise it."""

    def close(self) -> None:  # noqa: B027 - optional hook; most stores hold nothing to release
        """Release any connection. Safe to call more than once."""


class FileRecordStore(RecordStore):
    """One JSON file per record under ``.vcm/<kind>/<name>.json``."""

    name = "local"

    def __init__(self, base: str | Path):
        self.base = Path(base)

    def path(self, kind: str, name: str) -> Path:
        return self.base / kind / f"{check_name(kind, name)}.json"

    def save(self, kind: str, name: str, data: dict) -> Path:
        p = self.path(kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return p

    def load(self, kind: str, name: str) -> dict | None:
        p = self.path(kind, name)
        return json.loads(p.read_text()) if p.is_file() else None

    def list(self, kind: str) -> list[dict]:
        base = self.base / kind
        if not base.is_dir():
            return []
        return [json.loads(p.read_text()) for p in sorted(base.glob("*.json"))]

    def has(self, kind: str, name: str) -> bool:
        return self.path(kind, name).is_file()


__all__ = ["RecordStore", "FileRecordStore", "RecordError", "check_name", "SHARED_KINDS"]
