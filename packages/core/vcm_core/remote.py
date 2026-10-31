"""Remotes and locks: share one history across a team without pretending hardware merges.

A remote is a directory (a shared mount, an NFS export, a synced folder) holding the same
content-addressed object store as a local repository, plus refs, records and locks. Objects are
immutable and named by their hash, so copying them is idempotent and safe to repeat.

What is deliberately *not* here is merge. Two divergent layouts or node configurations have no
well-defined merge, and hardware teams do not merge them: they lock. So:

* pushes and pulls are **fast-forward only**; divergence is reported, with the way out;
* a file can be **locked** on the remote by one person for a while; pushes that change a file
  someone else holds are refused;
* tags and records (releases, units, racks, reviews) are never overwritten with different
  content. A disagreement is listed as a conflict for a human to resolve.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .objects import sha256_bytes
from .repo import Repository, VcmError

RECORD_KINDS = ("releases", "devices", "racks", "prs")
REMOTES_FILE = "remotes.json"


def identity() -> str:
    return os.environ.get("VCM_AUTHOR") or getpass.getuser()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ---- configuration -------------------------------------------------------------------
def _cfg_path(repo: Repository) -> Path:
    return repo.dir / REMOTES_FILE


def remotes(repo: Repository) -> dict[str, dict]:
    p = _cfg_path(repo)
    return json.loads(p.read_text()) if p.is_file() else {}


def add_remote(repo: Repository, name: str, url: str) -> dict:
    path = Path(url[len("file://"):] if url.startswith("file://") else url).expanduser().resolve()
    cfg = remotes(repo)
    cfg[name] = {"url": str(path)}
    _atomic_write(_cfg_path(repo), json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    return cfg[name]


class Remote:
    def __init__(self, path: str | Path):
        self.root = Path(path)

    @classmethod
    def of(cls, repo: Repository, name: str) -> Remote:
        cfg = remotes(repo).get(name)
        if not cfg:
            raise VcmError(f"no remote named {name!r} (add one with `vcm remote add`)")
        return cls(cfg["url"])

    def ensure(self) -> None:
        for sub in ("objects", "refs/heads", "refs/tags", "locks", "records"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return (self.root / "objects").is_dir()

    # objects
    def obj_path(self, digest: str) -> Path:
        return self.root / "objects" / digest[:2] / digest[2:]

    def has(self, digest: str) -> bool:
        return self.obj_path(digest).is_file()

    def digests(self):
        base = self.root / "objects"
        if not base.is_dir():
            return
        for sub in sorted(base.iterdir()):
            if sub.is_dir() and len(sub.name) == 2:
                for f in sorted(sub.iterdir()):
                    yield sub.name + f.name

    # refs
    def ref(self, kind: str, name: str) -> str | None:
        p = self.root / "refs" / kind / name
        return p.read_text().strip() if p.is_file() else None

    def refs(self, kind: str) -> dict[str, str]:
        base = self.root / "refs" / kind
        return {str(p.relative_to(base)): p.read_text().strip()
                for p in sorted(base.rglob("*")) if p.is_file()} if base.is_dir() else {}

    def set_ref(self, kind: str, name: str, value: str) -> None:
        _atomic_write(self.root / "refs" / kind / name, value + "\n")

    # a short-lived mutex around ref updates, so two pushes cannot interleave
    def mutex(self, timeout: float = 10.0):
        remote = self

        class _M:
            def __enter__(self_inner):
                deadline = time.monotonic() + timeout
                p = remote.root / "refs.lock"
                while True:
                    try:
                        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                        os.write(fd, f"{identity()} {os.getpid()}".encode())
                        os.close(fd)
                        return self_inner
                    except FileExistsError:
                        if time.monotonic() > deadline:
                            raise VcmError(f"remote is busy ({p} held); retry, or remove it if "
                                           "a push crashed") from None
                        time.sleep(0.05)

            def __exit__(self_inner, *exc):
                (remote.root / "refs.lock").unlink(missing_ok=True)
        return _M()


# ---- object transfer -----------------------------------------------------------------
def _copy_blob(src: Path, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".tmp-")
    os.close(fd)
    shutil.copyfile(src, tmp)
    os.chmod(tmp, 0o444)
    os.replace(tmp, dest)


def _upload(repo: Repository, remote: Remote) -> int:
    n = 0
    for d in repo.store.iter_digests():
        if not remote.has(d):
            _copy_blob(repo.store.path(d), remote.obj_path(d))
            n += 1
    return n


def _download(repo: Repository, remote: Remote) -> int:
    n = 0
    for d in remote.digests():
        if not repo.store.has(d):
            src = remote.obj_path(d)
            if sha256_bytes(src.read_bytes()) != d:
                raise VcmError(f"remote object {d[:12]} is corrupted; refusing to fetch it")
            _copy_blob(src, repo.store.path(d))
            n += 1
    return n


# ---- records -------------------------------------------------------------------------
def _sync_records(src_dir: Path, dst_dir: Path) -> tuple[list[str], list[str]]:
    """Copy records absent at the destination; list those that differ. Never overwrite."""
    copied, conflicts = [], []
    for kind in RECORD_KINDS:
        base = src_dir / kind
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*.json")):
            dest = dst_dir / kind / f.name
            if not dest.exists():
                _atomic_write(dest, f.read_text())
                copied.append(f"{kind}/{f.stem}")
            elif json.loads(dest.read_text()) != json.loads(f.read_text()):
                conflicts.append(f"{kind}/{f.stem}")
    return copied, conflicts


def _sync_evidence(src: Path, dst: Path) -> int:
    """Evidence indexes only ever grow, so they merge: entries the destination lacks (by content
    hash) are appended in time order and renumbered. The blobs travel with the objects."""
    added = 0
    if not src.is_dir():
        return 0
    for f in sorted(src.glob("*/*.json")):
        dest = dst / f.relative_to(src)
        theirs = json.loads(f.read_text())
        mine = json.loads(dest.read_text()) if dest.is_file() else []
        have = {e["hash"] for e in mine}
        new = [e for e in theirs if e["hash"] not in have]
        if not new:
            continue
        merged = sorted(mine + new, key=lambda e: (e.get("created_at", ""), e["hash"]))
        for i, e in enumerate(merged, start=1):
            e["version"] = i
        _atomic_write(dest, json.dumps(merged, indent=2))
        added += len(new)
    return added


# ---- locks ---------------------------------------------------------------------------
def _lock_path(remote: Remote, path: str) -> Path:
    return remote.root / "locks" / (sha256_bytes(path.encode())[:32] + ".json")


def _read_lock(p: Path) -> dict | None:
    if not p.is_file():
        return None
    lock = json.loads(p.read_text())
    lock["expired"] = _parse_iso(lock["expires_at"]) <= _now()
    return lock


def acquire_lock(remote: Remote, path: str, ttl_hours: float = 8.0, reason: str = "",
                 holder: str | None = None) -> dict:
    remote.ensure()
    holder = holder or identity()
    p = _lock_path(remote, path)
    lock = {"path": path, "holder": holder, "reason": reason, "acquired_at": _iso(_now()),
            "expires_at": _iso(_now() + timedelta(hours=ttl_hours))}
    existing = _read_lock(p)
    if existing and not existing["expired"] and existing["holder"] != holder:
        raise VcmError(f"{path} is locked by {existing['holder']} until "
                       f"{existing['expires_at']}" + (f" ({existing['reason']})"
                                                      if existing.get("reason") else ""))
    if existing is None:
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:  # lost a race with another acquirer
            return acquire_lock(remote, path, ttl_hours, reason, holder)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(lock, indent=2, sort_keys=True))
    else:  # ours to refresh, or someone else's that has expired
        _atomic_write(p, json.dumps(lock, indent=2, sort_keys=True))
    return lock


def release_lock(remote: Remote, path: str, holder: str | None = None,
                 force: bool = False) -> dict:
    holder = holder or identity()
    p = _lock_path(remote, path)
    lock = _read_lock(p)
    if lock is None:
        raise VcmError(f"{path} is not locked")
    if lock["holder"] != holder and not force and not lock["expired"]:
        raise VcmError(f"{path} is locked by {lock['holder']}, not {holder} (use --force to "
                       "break it; the break is audited)")
    p.unlink()
    return lock


def list_locks(remote: Remote) -> list[dict]:
    base = remote.root / "locks"
    if not base.is_dir():
        return []
    return sorted((x for x in (_read_lock(p) for p in base.glob("*.json")) if x),
                  key=lambda x: x["path"])


def blocking_locks(remote: Remote, paths, holder: str | None = None) -> list[dict]:
    """Live locks held by someone else on any of ``paths``."""
    holder = holder or identity()
    out = []
    for path in sorted(set(paths)):
        lock = _read_lock(_lock_path(remote, path))
        if lock and not lock["expired"] and lock["holder"] != holder:
            out.append(lock)
    return out


# ---- push / fetch / pull ---------------------------------------------------------------
def _changed_paths(repo: Repository, old: str | None, new: str) -> list[str]:
    if old is None or not repo.store.has(old):
        return sorted(repo.get_revision(new).artifacts)
    d = repo.diff_artifacts(old, new)
    return sorted({x["path"] for k in ("added", "removed", "modified") for x in d[k]})


def push(repo: Repository, name: str = "origin", branch: str | None = None) -> dict:
    remote = Remote.of(repo, name)
    remote.ensure()
    branch = branch or repo.current_branch
    if not branch:
        raise VcmError("detached HEAD: name the branch to push")
    local = repo.branches().get(branch)
    if not local:
        raise VcmError(f"no local branch {branch}")
    with remote.mutex():
        theirs = remote.ref("heads", branch)
        if theirs and theirs != local and not (repo.store.has(theirs)
                                               and repo.is_ancestor(theirs, local)):
            raise VcmError(f"push rejected: {name}/{branch} has revisions you do not have. Run "
                           "`vcm pull`; VCM does not merge hardware, so if the histories have "
                           "diverged, pick a side and re-apply your change.")
        blocked = blocking_locks(remote, _changed_paths(repo, theirs, local))
        if blocked:
            raise VcmError("push rejected: " + "; ".join(
                f"{b['path']} is locked by {b['holder']} until {b['expires_at']}"
                for b in blocked))
        uploaded = _upload(repo, remote)
        remote.set_ref("heads", branch, local)
        tag_conflicts, tags_pushed = [], []
        for tag, rid in repo.tags().items():
            cur = remote.ref("tags", tag)
            if cur is None:
                remote.set_ref("tags", tag, rid)
                tags_pushed.append(tag)
            elif cur != rid:
                tag_conflicts.append(tag)
        copied, conflicts = _sync_records(repo.dir, remote.root / "records")
        _sync_evidence(repo.dir / "evidence", remote.root / "evidence")
    repo.audit.append("push", f"{name}/{branch}", {"from": theirs, "to": local,
                                                  "objects": uploaded})
    return {"remote": name, "branch": branch, "from": theirs, "to": local,
            "objects": uploaded, "tags": tags_pushed, "tag_conflicts": tag_conflicts,
            "records": copied, "record_conflicts": conflicts,
            "up_to_date": theirs == local and not uploaded}


def fetch(repo: Repository, name: str = "origin") -> dict:
    remote = Remote.of(repo, name)
    if not remote.exists():
        raise VcmError(f"remote {name} at {remote.root} has nothing yet")
    downloaded = _download(repo, remote)
    heads = remote.refs("heads")
    for branch, rid in heads.items():
        p = repo.dir / "refs" / "remotes" / name / branch
        _atomic_write(p, rid + "\n")
    tags_added, tag_conflicts = [], []
    for tag, rid in remote.refs("tags").items():
        mine = repo.tags().get(tag)
        if mine is None:
            repo._set_ref("tags", tag, rid)
            tags_added.append(tag)
        elif mine != rid:
            tag_conflicts.append(tag)
    copied, conflicts = _sync_records(remote.root / "records", repo.dir)
    evidence = _sync_evidence(remote.root / "evidence", repo.dir / "evidence")
    return {"remote": name, "objects": downloaded, "evidence": evidence, "heads": heads, "tags": tags_added,
            "tag_conflicts": tag_conflicts, "records": copied, "record_conflicts": conflicts}


def pull(repo: Repository, name: str = "origin", branch: str | None = None) -> dict:
    res = fetch(repo, name)
    branch = branch or repo.current_branch
    if not branch:
        raise VcmError("detached HEAD: name the branch to pull")
    theirs = res["heads"].get(branch)
    mine = repo.branches().get(branch)
    res.update(branch=branch, before=mine, after=mine, fast_forward=False)
    if theirs is None or theirs == mine:
        return res
    if mine and not repo.is_ancestor(mine, theirs):
        if repo.is_ancestor(theirs, mine):
            return res  # we are ahead; nothing to take
        raise VcmError(f"{branch} has diverged from {name}/{branch}. VCM does not merge "
                       f"hardware: compare with `vcm diff {mine[:10]} {theirs[:10]}`, keep one "
                       "side, and re-apply the other change on top of it.")
    on_branch = repo.current_branch == branch
    if on_branch and repo.head_id():
        st = repo.status()
        if any(st.values()):
            raise VcmError("working tree has uncommitted changes; commit them before pulling")
    repo._set_ref("heads", branch, theirs)
    if on_branch:
        old = repo.get_revision(mine).artifacts if mine else {}
        new = repo.get_revision(theirs).artifacts
        for path in set(old) - set(new):
            (repo.root / path).unlink(missing_ok=True)
        for art in new.values():
            repo.store.copy_to(art.hash, repo.root / art.path)
    repo.audit.append("pull", f"{name}/{branch}", {"from": mine, "to": theirs})
    res.update(after=theirs, fast_forward=True)
    return res
