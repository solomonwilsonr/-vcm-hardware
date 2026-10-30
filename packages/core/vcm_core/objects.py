"""Content-addressed object store (sha256, deduplicating, streaming for large files)."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO

CHUNK = 1 << 20


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


class ObjectStore:
    """Stores blobs under ``root/ab/cdef...``; identical content is stored once."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)

    def path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:]

    def has(self, digest: str) -> bool:
        return self.path(digest).is_file()

    def _commit_tmp(self, tmp: str, digest: str) -> None:
        dest = self.path(digest)
        if dest.exists():
            os.unlink(tmp)
            return
        dest.parent.mkdir(exist_ok=True)
        os.chmod(tmp, 0o444)
        os.replace(tmp, dest)

    def put_bytes(self, data: bytes) -> str:
        digest = sha256_bytes(data)
        if not self.has(digest):
            fd, tmp = tempfile.mkstemp(dir=self.root / "tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            self._commit_tmp(tmp, digest)
        return digest

    def put_file(self, path: str | Path) -> tuple[str, int]:
        """Stream a file into the store; returns (digest, size)."""
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=self.root / "tmp")
        with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
            while chunk := src.read(CHUNK):
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
        digest = h.hexdigest()
        self._commit_tmp(tmp, digest)
        return digest, size

    def open(self, digest: str) -> BinaryIO:
        return open(self.path(digest), "rb")

    def read(self, digest: str) -> bytes:
        with self.open(digest) as f:
            return f.read()

    def copy_to(self, digest: str, dest: str | Path) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        with self.open(digest) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, CHUNK)

    def verify(self, digest: str) -> bool:
        """Re-hash the stored blob; False if missing or corrupted."""
        return self.has(digest) and sha256_file(self.path(digest)) == digest

    def iter_digests(self):
        for sub in sorted(self.root.iterdir()):
            if sub.is_dir() and len(sub.name) == 2:
                for f in sorted(sub.iterdir()):
                    yield sub.name + f.name
