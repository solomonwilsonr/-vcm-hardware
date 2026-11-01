"""A small cross-process file lock, standard library only.

Used where a read-then-write must not interleave with another process or thread: appending to the
audit log (the next sequence number depends on the last event) and committing (the new revision's
parent and the branch it moves must not change underneath it). The OS releases the lock if the
holder dies, so a crashed process cannot wedge the repository. The Windows branch is untested.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None


@contextlib.contextmanager
def file_lock(path: str | Path, shared: bool = False) -> Iterator[None]:
    """Hold an exclusive (or, with ``shared``, a shared) lock on ``path`` for the block.

    Each call opens its own file description, so the lock also serialises threads of one process.
    Do not nest a lock on the same path: it would wait on itself.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+b") as lk:
        if fcntl:
            fcntl.flock(lk, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        elif msvcrt:  # pragma: no cover
            lk.seek(0)
            msvcrt.locking(lk.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl:
                fcntl.flock(lk, fcntl.LOCK_UN)
            elif msvcrt:  # pragma: no cover
                lk.seek(0)
                msvcrt.locking(lk.fileno(), msvcrt.LK_UNLCK, 1)
