"""File locks with holder identity and expiry.

Hardware artifacts (a ``.kicad_pcb``, a CAD file, a firmware image) cannot be merged meaningfully,
so teams lock instead. A lock is one small JSON file, created with ``O_EXCL`` so two holders cannot
both win. The store is a directory: ``.vcm/locks`` locally, or ``locks/`` inside a shared remote.

The holder is a self-asserted string. Locks coordinate cooperating people; they are not an access
control. Identity is only verified where the REST API binds it to a bearer token.
"""
from __future__ import annotations

import json
import os
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
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def _file(self, path: str) -> Path:
        return self.dir / f"{sha256_bytes(path.encode())[:32]}.json"

    def _read(self, f: Path) -> dict | None:
        try:
            return json.loads(f.read_text())
        except (OSError, ValueError):
            return None

    def get(self, path: str, now: float | None = None) -> dict | None:
        """The live lock on ``path``, or None (expired locks do not count)."""
        lock = self._read(self._file(normalize(path)))
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
        self.dir.mkdir(parents=True, exist_ok=True)
        f = self._file(path)
        for _ in range(3):
            try:
                fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                cur = self._read(f)
                if cur and cur["expires_ts"] > t:
                    if cur["holder"] != holder:
                        raise VcmError(f"{path} is locked by {cur['holder']} until {cur['expires_at']}") from None
                    lock["acquired_at"] = cur["acquired_at"]
                    f.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
                    return lock
                f.unlink(missing_ok=True)  # expired or unreadable: reclaim and retry
                continue
            with os.fdopen(fd, "w") as out:
                out.write(json.dumps(lock, indent=2, sort_keys=True) + "\n")
            return lock
        raise VcmError(f"could not acquire lock on {path}")

    def release(self, path: str, holder: str, force: bool = False, now: float | None = None) -> dict:
        path = normalize(path)
        cur = self.get(path, now)
        if cur is None:
            raise VcmError(f"{path} is not locked")
        if cur["holder"] != holder and not force:
            raise VcmError(f"{path} is locked by {cur['holder']}, not {holder} (use --force to break it)")
        self._file(path).unlink(missing_ok=True)
        return cur

    def list(self, now: float | None = None) -> list[dict]:
        if not self.dir.is_dir():
            return []
        t = now or time.time()
        locks = [lk for f in sorted(self.dir.glob("*.json")) if (lk := self._read(f))]
        return sorted((lk for lk in locks if lk["expires_ts"] > t), key=lambda lk: lk["path"])

    def conflicts(self, paths, holder: str, now: float | None = None) -> list[dict]:
        """Live locks on ``paths`` that belong to someone other than ``holder``."""
        held = {lk["path"]: lk for lk in self.list(now)}
        return [held[p] for p in sorted(set(paths)) if p in held and held[p]["holder"] != holder]


def describe_conflicts(conflicts: list[dict]) -> str:
    return "; ".join(f"{c['path']} locked by {c['holder']} until {c['expires_at']}" for c in conflicts)
