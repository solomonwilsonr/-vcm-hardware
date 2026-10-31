"""Append-only, hash-chained audit log of everything that changes a repository's state.

Each event records who did what to which subject, and carries the hash of the event before it, so
editing or deleting any past event breaks every hash after it. A chain alone can still be rewritten
wholesale by someone with write access to ``.vcm/``; anchoring its head hash somewhere they cannot
rewrite (a signed release manifest, a remote) is what makes that detectable. Releases record the
audit head for exactly that reason.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .model import canonical_json
from .objects import sha256_bytes

GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_hash(event: dict) -> str:
    return sha256_bytes(canonical_json({k: v for k, v in event.items() if k != "hash"}))


class AuditLog:
    """``events.jsonl`` under a repository's ``.vcm/audit/``: one JSON event per line."""

    def __init__(self, vcm_dir: str | Path):
        self.path = Path(vcm_dir) / "audit" / "events.jsonl"

    def events(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [json.loads(ln) for ln in self.path.read_text().splitlines() if ln.strip()]

    def last(self) -> dict | None:
        """The newest event, read from the end of the file so appends stay O(1) in log size."""
        if not self.path.is_file():
            return None
        with open(self.path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            chunk, pos = b"", end
            while pos > 0:
                step = min(4096, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + chunk
                lines = [ln for ln in chunk.splitlines() if ln.strip()]
                if len(lines) > 1 or (lines and pos == 0):
                    return json.loads(lines[-1])
        return None

    def head(self) -> str:
        ev = self.last()
        return ev["hash"] if ev else GENESIS

    def append(self, action: str, subject: str, data: dict | None = None,
               actor: str | None = None) -> dict:
        prev = self.last()
        event = {"seq": (prev["seq"] if prev else 0) + 1, "at": _now(),
                 "actor": actor or os.environ.get("VCM_AUTHOR", "unknown"),
                 "action": action, "subject": subject, "data": data or {},
                 "prev": prev["hash"] if prev else GENESIS}
        event["hash"] = event_hash(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def verify(self) -> dict:
        """Re-walk the chain. Reports the first broken link rather than every downstream one."""
        prev = GENESIS
        for i, ev in enumerate(self.events(), start=1):
            if ev.get("seq") != i:
                return {"ok": False, "events": i - 1, "broken_at": i,
                        "reason": f"sequence gap: expected {i}, found {ev.get('seq')}"}
            if ev.get("prev") != prev:
                return {"ok": False, "events": i - 1, "broken_at": i,
                        "reason": "event does not chain to the one before it"}
            if event_hash(ev) != ev.get("hash"):
                return {"ok": False, "events": i - 1, "broken_at": i,
                        "reason": "event content does not match its recorded hash"}
            prev = ev["hash"]
        return {"ok": True, "events": len(self.events()), "head": prev}
