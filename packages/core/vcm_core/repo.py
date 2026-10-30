"""Repository: init / commit / log / checkout / diff / refs / evidence."""
from __future__ import annotations

import fnmatch
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .categories import categorize
from .model import Artifact, Revision, canonical_json
from .objects import ObjectStore

VCM_DIR = ".vcm"
ALWAYS_IGNORED = {VCM_DIR, ".git", "__pycache__", ".DS_Store"}


class VcmError(Exception):
    pass


class NothingToCommit(VcmError):
    pass


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Repository:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.dir = self.root / VCM_DIR
        if not self.dir.is_dir():
            raise VcmError(f"not a vcm repository: {self.root}")
        self.store = ObjectStore(self.dir / "objects")

    # ---- lifecycle -------------------------------------------------
    @classmethod
    def init(cls, path: str | Path = ".", branch: str = "main") -> Repository:
        root = Path(path).resolve()
        d = root / VCM_DIR
        if d.exists():
            raise VcmError(f"repository already exists at {root}")
        for sub in ("objects", "refs/heads", "refs/tags", "evidence"):
            (d / sub).mkdir(parents=True)
        (d / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        return cls(root)

    @classmethod
    def discover(cls, start: str | Path = ".") -> Repository:
        p = Path(start).resolve()
        for cand in (p, *p.parents):
            if (cand / VCM_DIR).is_dir():
                return cls(cand)
        raise VcmError("not inside a vcm repository (run `vcm init`)")

    # ---- project config and locks ----------------------------------
    def config(self) -> dict:
        f = self.dir / "config.json"
        return json.loads(f.read_text()) if f.is_file() else {}

    def save_config(self, cfg: dict) -> None:
        (self.dir / "config.json").write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")

    @property
    def locks(self):
        from .locks import LockStore  # lazy: locks imports this module
        return LockStore(self.dir / "locks")

    def protected_branches(self) -> list[str]:
        return list(self.config().get("protected_branches", []))

    # ---- refs ------------------------------------------------------
    def _head_raw(self) -> str:
        return (self.dir / "HEAD").read_text().strip()

    @property
    def current_branch(self) -> str | None:
        raw = self._head_raw()
        return raw[len("ref: refs/heads/"):] if raw.startswith("ref: ") else None

    def _ref_path(self, kind: str, name: str) -> Path:
        return self.dir / "refs" / kind / name

    def head_id(self) -> str | None:
        raw = self._head_raw()
        if raw.startswith("ref: "):
            p = self.dir / raw[5:]
            return p.read_text().strip() if p.exists() else None
        return raw

    def _set_ref(self, kind: str, name: str, rev_id: str) -> None:
        p = self._ref_path(kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rev_id + "\n")

    def _list_refs(self, kind: str) -> dict[str, str]:
        base = self.dir / "refs" / kind
        return {str(p.relative_to(base)): p.read_text().strip()
                for p in sorted(base.rglob("*")) if p.is_file()}

    def branches(self) -> dict[str, str]:
        return self._list_refs("heads")

    def tags(self) -> dict[str, str]:
        return self._list_refs("tags")

    def tag(self, name: str, rev: str = "HEAD") -> str:
        rid = self.resolve(rev)
        if self._ref_path("tags", name).exists():
            raise VcmError(f"tag exists: {name}")
        self._set_ref("tags", name, rid)
        return rid

    def branch(self, name: str, start: str = "HEAD") -> str:
        if self._ref_path("heads", name).exists():
            raise VcmError(f"branch exists: {name}")
        rid = self.resolve(start)
        self._set_ref("heads", name, rid)
        return rid

    # ---- revisions -------------------------------------------------
    def resolve(self, spec: str = "HEAD") -> str:
        """Resolve HEAD, HEAD~N, branch, tag, full id or unique id prefix."""
        base, _, n = spec.partition("~")
        rid = self._resolve_base(base or "HEAD")
        for _ in range(int(n) if n else 0):
            parents = self.get_revision(rid).parents
            if not parents:
                raise VcmError(f"{spec}: no such ancestor")
            rid = parents[0]
        return rid

    def _resolve_base(self, base: str) -> str:
        if base == "HEAD":
            rid = self.head_id()
            if not rid:
                raise VcmError("no commits yet")
            return rid
        for kind in ("heads", "tags"):
            p = self._ref_path(kind, base)
            if p.is_file():
                return p.read_text().strip()
        if len(base) >= 4 and all(c in "0123456789abcdef" for c in base):
            hits = [d for d in self.store.iter_digests() if d.startswith(base)
                    and self._is_revision(d)]
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise VcmError(f"ambiguous revision: {base}")
        raise VcmError(f"unknown revision: {base}")

    def _is_revision(self, digest: str) -> bool:
        try:
            return json.loads(self.store.read(digest)).get("type") == "revision"
        except (ValueError, OSError):
            return False

    def get_revision(self, spec: str) -> Revision:
        rid = spec if len(spec) == 64 and self.store.has(spec) else self.resolve(spec)
        body = json.loads(self.store.read(rid))
        return Revision.from_body(rid, body)

    def all_revisions(self) -> list[Revision]:
        return [Revision.from_body(d, json.loads(self.store.read(d)))
                for d in self.store.iter_digests() if self._is_revision(d)]

    def log(self, start: str = "HEAD", limit: int | None = None) -> list[Revision]:
        """First-parent history, newest first."""
        out: list[Revision] = []
        rid: str | None = self.resolve(start)
        while rid and (limit is None or len(out) < limit):
            rev = self.get_revision(rid)
            out.append(rev)
            rid = rev.parents[0] if rev.parents else None
        return out

    def ancestors(self, rev: str) -> list[str]:
        return [r.id for r in self.log(rev)]

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self.resolve(ancestor) in self.ancestors(descendant)

    # ---- working tree ----------------------------------------------
    def _ignore_patterns(self) -> list[str]:
        f = self.root / ".vcmignore"
        if not f.exists():
            return []
        return [ln.strip() for ln in f.read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]

    def scan(self) -> dict[str, Path]:
        patterns = self._ignore_patterns()
        found: dict[str, Path] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in ALWAYS_IGNORED)
            for fn in sorted(filenames):
                if fn in ALWAYS_IGNORED or fn.endswith(".pyc"):
                    continue
                full = Path(dirpath) / fn
                rel = full.relative_to(self.root).as_posix()
                if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(fn, p) for p in patterns):
                    continue
                found[rel] = full
        return found

    def commit(self, message: str, author: str | None = None,
               timestamp: str | None = None, allow_empty: bool = False) -> Revision:
        parent = self.head_id()
        author = author or os.environ.get("VCM_AUTHOR", "unknown")
        branch = self.current_branch
        if branch is None:
            raise VcmError("detached HEAD: create a branch before committing")
        if branch in self.protected_branches():
            raise VcmError(f"branch {branch} is protected: change it through a merged pull request")
        artifacts: dict[str, Artifact] = {}
        for rel, full in self.scan().items():
            digest, size = self.store.put_file(full)
            artifacts[rel] = Artifact(rel, categorize(rel), digest, size)
        prev = self.get_revision(parent).artifacts if parent else {}
        prev_hashes = {k: v.hash for k, v in prev.items()}
        new_hashes = {k: v.hash for k, v in artifacts.items()}
        if parent and not allow_empty and prev_hashes == new_hashes:
            raise NothingToCommit("no changes since last revision")
        changed = {p for p in prev_hashes.keys() | new_hashes.keys()
                   if prev_hashes.get(p) != new_hashes.get(p)}
        held = self.locks.conflicts(changed, author)
        if held:
            from .locks import describe_conflicts
            raise VcmError("cannot commit, locked by someone else: " + describe_conflicts(held))
        rev = Revision("", [parent] if parent else [], message, author,
                       timestamp or now_iso(), branch, artifacts)
        rev.id = self.store.put_bytes(canonical_json(rev.body()))
        self._set_ref("heads", branch, rev.id)
        return rev

    def status(self) -> dict[str, list[str]]:
        """Working-tree changes relative to HEAD."""
        head = self.head_id()
        prev = self.get_revision(head).artifacts if head else {}
        from .objects import sha256_file
        cur = self.scan()
        added = sorted(set(cur) - set(prev))
        removed = sorted(set(prev) - set(cur))
        modified = sorted(p for p in set(cur) & set(prev)
                          if sha256_file(cur[p]) != prev[p].hash)
        return {"added": added, "removed": removed, "modified": modified}

    def checkout(self, spec: str, force: bool = False) -> Revision:
        st = self.status() if self.head_id() else {"added": [], "removed": [], "modified": []}
        if not force and any(st.values()):
            raise VcmError("working tree has uncommitted changes (use --force)")
        rev = self.get_revision(spec)
        self._materialize(rev)
        if (self._ref_path("heads", spec)).is_file():
            (self.dir / "HEAD").write_text(f"ref: refs/heads/{spec}\n")
        else:
            (self.dir / "HEAD").write_text(rev.id + "\n")
        return rev

    def _materialize(self, rev: Revision) -> None:
        """Make the working tree match ``rev`` (files only in the old HEAD are removed)."""
        current = self.get_revision(self.head_id()).artifacts if self.head_id() else {}
        for path in set(current) - set(rev.artifacts):
            (self.root / path).unlink(missing_ok=True)
        for art in rev.artifacts.values():
            self.store.copy_to(art.hash, self.root / art.path)

    def fast_forward(self, branch: str, rev: str) -> Revision:
        """Move ``branch`` forward to ``rev``. Refuses anything that is not a pure fast-forward;
        VCM has no merge. If ``branch`` is checked out the working tree is updated too, and must
        be clean."""
        target = self.get_revision(rev)
        p = self._ref_path("heads", branch)
        old = p.read_text().strip() if p.is_file() else None
        if old and old != target.id and old not in self.ancestors(target.id):
            raise VcmError(f"{branch} cannot fast-forward to {target.id[:10]}: histories diverged "
                           "(VCM has no merge; pick one side)")
        if branch == self.current_branch:
            if self.head_id() and any(self.status().values()):
                raise VcmError("working tree has uncommitted changes")
            self._materialize(target)
        self._set_ref("heads", branch, target.id)
        return target

    # ---- artifacts and diff ----------------------------------------
    def read_artifact(self, rev: str | Revision, path: str) -> bytes:
        r = rev if isinstance(rev, Revision) else self.get_revision(rev)
        if path not in r.artifacts:
            raise VcmError(f"{path} not in revision {r.id[:10]}")
        return self.store.read(r.artifacts[path].hash)

    def files(self, rev: str | Revision) -> dict[str, bytes]:
        r = rev if isinstance(rev, Revision) else self.get_revision(rev)
        return {p: self.store.read(a.hash) for p, a in r.artifacts.items()}

    def diff_artifacts(self, a: str, b: str) -> dict:
        ra, rb = self.get_revision(a), self.get_revision(b)
        pa, pb = ra.artifacts, rb.artifacts
        return {
            "from": ra.id, "to": rb.id,
            "added": [{"path": p, "category": pb[p].category} for p in sorted(set(pb) - set(pa))],
            "removed": [{"path": p, "category": pa[p].category} for p in sorted(set(pa) - set(pb))],
            "modified": [{"path": p, "category": pb[p].category,
                          "old_hash": pa[p].hash, "new_hash": pb[p].hash}
                         for p in sorted(set(pa) & set(pb)) if pa[p].hash != pb[p].hash],
        }

    # ---- evidence (versioned results attached to a revision) -------
    def _evidence_path(self, rev_id: str, kind: str) -> Path:
        return self.dir / "evidence" / rev_id / f"{kind}.json"

    def attach_evidence(self, rev: str, kind: str, data: dict) -> dict:
        rid = self.resolve(rev)
        digest = self.store.put_bytes(canonical_json(data))
        p = self._evidence_path(rid, kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        entries = json.loads(p.read_text()) if p.exists() else []
        entry = {"version": len(entries) + 1, "hash": digest, "created_at": now_iso()}
        entries.append(entry)
        p.write_text(json.dumps(entries, indent=2))
        return entry

    def evidence_history(self, rev: str, kind: str) -> list[dict]:
        p = self._evidence_path(self.resolve(rev), kind)
        return json.loads(p.read_text()) if p.exists() else []

    def latest_evidence(self, rev: str, kind: str) -> dict | None:
        hist = self.evidence_history(rev, kind)
        if not hist:
            return None
        data = json.loads(self.store.read(hist[-1]["hash"]))
        return {**data, "_evidence": hist[-1]}

    # ---- named JSON records (releases, devices, pull requests) -----
    def _record_path(self, kind: str, name: str) -> Path:
        if not name or name.startswith(".") or any(c in name for c in "/\\\0"):
            raise VcmError(f"invalid {kind[:-1]} name: {name!r}")
        return self.dir / kind / f"{name}.json"

    def save_record(self, kind: str, name: str, data: dict) -> Path:
        p = self._record_path(kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        return p

    def load_record(self, kind: str, name: str) -> dict:
        p = self._record_path(kind, name)
        if not p.is_file():
            raise VcmError(f"no such {kind[:-1]}: {name}")
        return json.loads(p.read_text())

    def list_records(self, kind: str) -> list[dict]:
        base = self.dir / kind
        return [json.loads(p.read_text()) for p in sorted(base.glob("*.json"))] if base.is_dir() else []
