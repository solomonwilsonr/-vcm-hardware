"""Snapshot / revision data model."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Artifact:
    path: str
    category: str
    hash: str
    size: int


@dataclass
class Revision:
    """A snapshot: a manifest of artifacts plus lineage. ``id`` is the sha256 of its canonical JSON."""

    id: str
    parents: list[str]
    message: str
    author: str
    timestamp: str
    branch: str
    artifacts: dict[str, Artifact] = field(default_factory=dict)

    def body(self) -> dict:
        return {
            "type": "revision",
            "parents": self.parents,
            "message": self.message,
            "author": self.author,
            "timestamp": self.timestamp,
            "branch": self.branch,
            "artifacts": [
                {"path": a.path, "category": a.category, "hash": a.hash, "size": a.size}
                for a in sorted(self.artifacts.values(), key=lambda a: a.path)
            ],
        }

    @classmethod
    def from_body(cls, rev_id: str, body: dict) -> Revision:
        arts = {a["path"]: Artifact(**a) for a in body["artifacts"]}
        return cls(rev_id, body["parents"], body["message"], body["author"],
                   body["timestamp"], body["branch"], arts)

    def by_category(self, category: str) -> list[Artifact]:
        return [a for a in self.artifacts.values() if a.category == category]

    def summary(self) -> dict:
        return {"id": self.id, "short": self.id[:10], "parents": self.parents,
                "message": self.message, "author": self.author,
                "timestamp": self.timestamp, "branch": self.branch,
                "artifact_count": len(self.artifacts)}
