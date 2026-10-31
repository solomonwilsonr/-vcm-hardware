"""Storage for named JSON records (releases, devices, pull requests, locks) and the event feed.

The default is local files under ``.vcm``. A shared store (Appwrite TablesDB, in ``vcm_graph``)
registers itself here, so the core stays standard-library only and never imports it. Pick one with
``records_backend`` in ``.vcm/config.json`` or ``$VCM_RECORDS_BACKEND``.

Every backend offers an *exclusive* create. That is what makes a lock atomic: two holders racing
for the same file cannot both succeed.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .repo import VcmError

ENV = "VCM_RECORDS_BACKEND"


class RecordBackend(Protocol):
    name: str

    def get(self, kind: str, name: str) -> dict | None: ...
    def put(self, kind: str, name: str, data: dict) -> None: ...
    def create(self, kind: str, name: str, data: dict) -> bool: ...
    def delete(self, kind: str, name: str) -> bool: ...
    def names(self, kind: str) -> list[str]: ...
    def list(self, kind: str) -> list[dict]: ...
    def events(self, after: str = "", limit: int = 200) -> list[dict]: ...


def event_at() -> str:
    """Sortable timestamp with microseconds (events are ordered and paged by this string)."""
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1e6):06d}Z"


def make_event(kind: str, name: str, action: str, data: dict | None = None) -> dict:
    """A small, payload-free notification: enough to know *what* changed, never the content."""
    d = data or {}
    actor = d.get("holder") or d.get("merged_by") or d.get("author") or ""
    return {"id": secrets.token_hex(8), "at": event_at(), "kind": kind, "name": name,
            "action": action, "actor": actor}


class LocalRecords:
    """``<base>/<kind>/<name>.json`` files, plus an append-only ``<base>/events.jsonl``."""

    name = "local"

    def __init__(self, base: str | Path):
        self.base = Path(base)

    def _path(self, kind: str, name: str) -> Path:
        if not name or name.startswith(".") or any(c in name for c in "/\\\0"):
            raise VcmError(f"invalid {kind[:-1]} name: {name!r}")
        return self.base / kind / f"{name}.json"

    def _emit(self, kind: str, name: str, action: str, data: dict | None) -> None:
        if self.base.name != ".vcm":  # only a repository has a feed; a remote's files do not
            return
        self.base.mkdir(parents=True, exist_ok=True)
        with open(self.base / "events.jsonl", "a") as f:
            f.write(json.dumps(make_event(kind, name, action, data)) + "\n")

    def get(self, kind: str, name: str) -> dict | None:
        p = self._path(kind, name)
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def put(self, kind: str, name: str, data: dict) -> None:
        p = self._path(kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        self._emit(kind, name, "updated" if existed else "created", data)

    def create(self, kind: str, name: str, data: dict) -> bool:
        p = self._path(kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        self._emit(kind, name, "created", data)
        return True

    def delete(self, kind: str, name: str) -> bool:
        p = self._path(kind, name)
        if not p.exists():
            return False
        p.unlink(missing_ok=True)
        self._emit(kind, name, "deleted", None)
        return True

    def names(self, kind: str) -> list[str]:
        d = self.base / kind
        return [p.stem for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def list(self, kind: str) -> list[dict]:
        return [r for n in self.names(kind) if (r := self.get(kind, n)) is not None]

    def events(self, after: str = "", limit: int = 200) -> list[dict]:
        f = self.base / "events.jsonl"
        if not f.is_file():
            return []
        out = []
        for line in f.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e["at"] > after:
                out.append(e)
        return sorted(out, key=lambda e: e["at"])[-limit:]


_FACTORIES: dict[str, Callable] = {}


def register_backend(name: str, factory: Callable) -> None:
    """``factory(repo) -> RecordBackend``. Called by the package that implements the backend."""
    _FACTORIES[name] = factory


def open_backend(repo) -> RecordBackend:
    choice = (os.environ.get(ENV) or repo.config().get("records_backend") or "local").lower()
    if choice == "local":
        return LocalRecords(repo.dir)
    if choice not in _FACTORIES:
        raise VcmError(f"records backend {choice!r} is not available here "
                       f"(known: local{''.join(', ' + n for n in sorted(_FACTORIES))}). "
                       "For 'appwrite', import vcm_graph and install the appwrite extra")
    return _FACTORIES[choice](repo)


def copy_records(src: RecordBackend, dst: RecordBackend,
                 kinds: tuple[str, ...] = ("releases", "devices", "prs")) -> dict:
    """Copy records ``src`` -> ``dst``, never overwriting. Returns what moved and what differed."""
    report: dict = {"copied": [], "skipped": [], "conflicts": []}
    for kind in kinds:
        for name in src.names(kind):
            mine = src.get(kind, name)
            if dst.create(kind, name, mine):
                report["copied"].append(f"{kind}/{name}")
            elif dst.get(kind, name) == mine:
                report["skipped"].append(f"{kind}/{name}")
            else:
                report["conflicts"].append(f"{kind}/{name}")
    return report
