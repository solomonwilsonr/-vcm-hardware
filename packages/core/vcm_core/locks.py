"""File locks with holder identity and expiry.

Hardware artifacts (a ``.kicad_pcb``, a CAD file, a firmware image) cannot be merged meaningfully,
so teams lock instead. A lock is one small record, created with an *exclusive* create (``O_EXCL``
on disk, a duplicate row id in Appwrite) so two holders cannot both win. The store is
``.vcm/locks`` locally, ``locks/`` inside a filesystem remote, or the shared Appwrite table.

The holder is a self-asserted string. Locks coordinate cooperating people; they are not an access
control. Identity is only verified where the REST API binds it to a bearer token.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .objects import sha256_bytes
from .repo import VcmError

DEFAULT_TTL = 8 * 3600


def normalize(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise VcmError(f"invalid lock path: {path!r} (use a path relative to the repository)")
    return p.as_posix()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class LockStore:
    """Locks kept as records of kind ``locks`` in a record backend.

    ``LockStore(path)`` keeps them as files in that directory (``.vcm/locks``, or a remote's
    ``locks/``); ``LockStore(backend)`` uses any ``RecordBackend``, so a team can share one set of
    locks through Appwrite. Either way acquisition goes through an exclusive create.
    """

    KIND = "locks"

    def __init__(self, store):
        if isinstance(store, (str, Path)):
            from .records import LocalRecords
            store = LocalRecords(Path(store).parent)  # <parent>/locks/<key>.json
        self.store = store
        self.kind = self.KIND

    def _key(self, path: str) -> str:
        return sha256_bytes(path.encode())[:32]

    def get(self, path: str, now: float | None = None) -> dict | None:
        """The live lock on ``path``, or None (expired locks do not count)."""
        lock = self.store.get(self.kind, self._key(normalize(path)))
        return lock if lock and lock["expires_ts"] > (now or time.time()) else None

    def acquire(self, path: str, holder: str, ttl: int = DEFAULT_TTL, note: str = "",
                now: float | None = None) -> dict:
        """Take the lock, or renew it if ``holder`` already owns it. Raises if someone else does."""
        if not holder or holder == "unknown":
            raise VcmError("a lock needs a holder identity (--holder or $VCM_AUTHOR)")
        if ttl <= 0:
            raise VcmError("lock ttl must be positive")
        path = normalize(path)
        t = now or time.time()
        lock = {"path": path, "holder": holder, "acquired_at": _iso(t), "expires_at": _iso(t + ttl),
                "expires_ts": t + ttl, "note": note}
        key = self._key(path)
        for _ in range(3):
            if self.store.create(self.kind, key, lock):
                return lock
            cur = self.store.get(self.kind, key)
            if cur and cur["expires_ts"] > t:
                if cur["holder"] != holder:
                    raise VcmError(
                        f"{path} is locked by {cur['holder']} until {cur['expires_at']}") from None
                lock["acquired_at"] = cur["acquired_at"]
                self.store.put(self.kind, key, lock)
                return lock
            self.store.delete(self.kind, key)  # expired or unreadable: reclaim and retry
        raise VcmError(f"could not acquire lock on {path}")

    def release(self, path: str, holder: str, force: bool = False, now: float | None = None) -> dict:
        path = normalize(path)
        cur = self.get(path, now)
        if cur is None:
            raise VcmError(f"{path} is not locked")
        if cur["holder"] != holder and not force:
            raise VcmError(f"{path} is locked by {cur['holder']}, not {holder} (use --force to break it)")
        self.store.delete(self.kind, self._key(path))
        return cur

    def list(self, now: float | None = None) -> list[dict]:
        t = now or time.time()
        locks = self.store.list(self.kind)
        return sorted((lk for lk in locks if lk["expires_ts"] > t), key=lambda lk: lk["path"])

    def conflicts(self, paths, holder: str, now: float | None = None) -> list[dict]:
        """Live locks on ``paths`` that belong to someone other than ``holder``."""
        held = {lk["path"]: lk for lk in self.list(now)}
        return [held[p] for p in sorted(set(paths)) if p in held and held[p]["holder"] != holder]


def describe_conflicts(conflicts: list[dict]) -> str:
    return "; ".join(f"{c['path']} locked by {c['holder']} until {c['expires_at']}" for c in conflicts)
