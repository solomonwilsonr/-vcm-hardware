"""Sync with a shared remote: push, pull, clone. Filesystem transport only.

A remote is a directory (a mounted share, a synced folder, a bare path) laid out like ``.vcm``:
``objects/``, ``refs/heads``, ``refs/tags``, ``evidence/``, ``releases/``, ``devices/`` and
``locks/``. Objects are content-addressed, so copying is idempotent and safe to retry, and every
blob is re-hashed on the way across so corruption does not propagate.

There is no merge. A branch only ever moves by fast-forward; divergence is reported, never
resolved. Network transports (S3, HTTP, SSH) are not implemented.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .locks import LockStore, describe_conflicts
from .objects import ObjectStore
from .repo import Repository, VcmError

RECORD_KINDS = ("releases", "devices")  # create-only records that travel with the history


@dataclass
class Side:
    """Either end of a transfer: a ``.vcm``-shaped directory plus its object store."""

    dir: Path
    store: ObjectStore


def _side(path: Path, create: bool = False) -> Side:
    """Open a remote directory. Only a push creates the layout; reads never write to a remote."""
    if create:
        for sub in ("refs/heads", "refs/tags", "evidence", *RECORD_KINDS):
            (path / sub).mkdir(parents=True, exist_ok=True)
        return Side(path, ObjectStore(path / "objects"))
    return Side(path, _ReadOnlyStore(path / "objects"))


class _ReadOnlyStore(ObjectStore):
    def __init__(self, root: Path):  # no mkdir: reading must not modify the remote
        self.root = root


def _repo_side(repo: Repository) -> Side:
    return Side(repo.dir, repo.store)


# ---- remote configuration --------------------------------------------------------------
def remote_path(url: str) -> Path:
    if url.startswith("file://"):
        url = url[len("file://"):]
    if "://" in url:
        raise VcmError(f"unsupported remote transport: {url!r} (only filesystem paths work; "
                       "S3/HTTP/SSH are planned)")
    return Path(url).expanduser().resolve()


def list_remotes(repo: Repository) -> dict[str, str]:
    return dict(repo.config().get("remotes", {}))


def add_remote(repo: Repository, name: str, url: str) -> None:
    remote_path(url)  # validate the scheme
    cfg = repo.config()
    remotes = cfg.setdefault("remotes", {})
    if name in remotes:
        raise VcmError(f"remote exists: {name}")
    remotes[name] = str(remote_path(url))
    repo.save_config(cfg)


def remove_remote(repo: Repository, name: str) -> None:
    cfg = repo.config()
    if name not in cfg.get("remotes", {}):
        raise VcmError(f"no such remote: {name}")
    del cfg["remotes"][name]
    repo.save_config(cfg)


def resolve_remote(repo: Repository, name_or_url: str) -> Path:
    return remote_path(list_remotes(repo).get(name_or_url, name_or_url))


def remote_locks(path: Path) -> LockStore:
    return LockStore(path / "locks")


# ---- graph helpers over a bare store ---------------------------------------------------
def _body(side: Side, rid: str) -> dict:
    if not side.store.verify(rid):
        raise VcmError(f"object {rid[:10]} is missing or corrupted")
    return json.loads(side.store.read(rid))


def _closure(side: Side, tip: str, stop: set[str] | None = None) -> list[str]:
    """Revision ids reachable from ``tip`` (newest first), not descending below ``stop``."""
    seen: list[str] = []
    todo = [tip]
    while todo:
        rid = todo.pop(0)
        if rid in seen or (stop and rid in stop):
            continue
        seen.append(rid)
        todo.extend(_body(side, rid)["parents"])
    return seen


def _refs(side: Side, kind: str) -> dict[str, str]:
    base = side.dir / "refs" / kind
    return {str(p.relative_to(base)): p.read_text().strip()
            for p in sorted(base.rglob("*")) if p.is_file()} if base.is_dir() else {}


def _set_ref(side: Side, kind: str, name: str, rid: str) -> None:
    p = side.dir / "refs" / kind / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(rid + "\n")


def _copy_object(src: Side, dst: Side, digest: str) -> int:
    if dst.store.has(digest):
        return 0
    if not src.store.verify(digest):
        raise VcmError(f"object {digest[:10]} is missing or corrupted at the source")
    dst.store.put_bytes(src.store.read(digest))
    return 1


def _copy_evidence(src: Side, dst: Side, rid: str) -> int:
    """Evidence indexes are append-only per revision; copy only where the target has none."""
    n = 0
    base = src.dir / "evidence" / rid
    for idx in sorted(base.glob("*.json")) if base.is_dir() else []:
        entries = json.loads(idx.read_text())
        for e in entries:
            n += _copy_object(src, dst, e["hash"])
        out = dst.dir / "evidence" / rid / idx.name
        if not out.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(idx, out)
    return n


def _copy_revisions(src: Side, dst: Side, rids: list[str]) -> int:
    n = 0
    for rid in rids:
        body = _body(src, rid)
        n += _copy_object(src, dst, rid)
        for a in body["artifacts"]:
            n += _copy_object(src, dst, a["hash"])
        n += _copy_evidence(src, dst, rid)
    return n


def _merge_record(kind: str, name: str, mine: dict, theirs: dict) -> dict | None:
    """Return the record to write on the destination, or None if it already matches."""
    if mine == theirs:
        return None
    if kind == "releases":
        strip = lambda d: {k: v for k, v in d.items() if k != "signatures"}  # noqa: E731
        if strip(mine) != strip(theirs):
            raise VcmError(f"release {name} differs between the two sides; releases are immutable")
        sigs = {s["key_id"]: s for s in theirs.get("signatures", [])}
        sigs.update({s["key_id"]: s for s in mine.get("signatures", [])})
        merged = {**theirs, "signatures": sorted(sigs.values(), key=lambda s: s["key_id"])}
        return None if merged == theirs else merged
    raise VcmError(f"{kind[:-1]} {name} differs between the two sides; records are create-only")


def _sync_records(src: Side, dst: Side) -> int:
    n = 0
    for kind in RECORD_KINDS:
        (dst.dir / kind).mkdir(parents=True, exist_ok=True)
        for f in sorted((src.dir / kind).glob("*.json")):
            mine = json.loads(f.read_text())
            out = dst.dir / kind / f.name
            if out.exists():
                merged = _merge_record(kind, f.stem, mine, json.loads(out.read_text()))
                if merged is None:
                    continue
                out.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
            else:
                shutil.copyfile(f, out)
                svg = f.with_suffix(".svg")
                if svg.is_file():
                    shutil.copyfile(svg, out.with_suffix(".svg"))
            n += 1
    return n


def _changed_paths(side: Side, rid: str) -> set[str]:
    """Artifact paths a revision changed relative to its first parent."""
    body = _body(side, rid)
    cur = {a["path"]: a["hash"] for a in body["artifacts"]}
    prev: dict[str, str] = {}
    if body["parents"]:
        prev = {a["path"]: a["hash"] for a in _body(side, body["parents"][0])["artifacts"]}
    return {p for p in cur.keys() | prev.keys() if cur.get(p) != prev.get(p)}


# ---- push / pull / clone ---------------------------------------------------------------
def push(repo: Repository, remote: str, branch: str | None = None, force: bool = False,
         holder: str | None = None) -> dict:
    """Send branch(es), tags, reachable objects and records to the remote.

    Refuses non-fast-forward updates (``force`` overrides) and any revision that changes a file
    another holder has locked on the remote.
    """
    dst_path = resolve_remote(repo, remote)
    dst, src = _side(dst_path, create=True), _repo_side(repo)
    heads = repo.branches()
    if branch:
        if branch not in heads:
            raise VcmError(f"no such branch: {branch}")
        heads = {branch: heads[branch]}
    holder = holder or "unknown"
    report = {"remote": str(dst_path), "branches": {}, "tags": {}, "objects": 0, "records": 0}
    remote_heads = _refs(dst, "heads")
    plan: list[tuple[str, str, list[str]]] = []
    for name, tip in sorted(heads.items()):
        old = remote_heads.get(name)
        if old == tip:
            report["branches"][name] = "up to date"
            continue
        known = set(_closure(dst, old)) if old and dst.store.has(old) else set()
        new = _closure(src, tip, stop=known)
        if old and not force and (old not in _closure(src, tip) if src.store.has(old) else True):
            raise VcmError(f"push of {name} rejected: remote {name} is not an ancestor of yours "
                           "(pull first; VCM does not merge)")
        held = remote_locks(dst_path).conflicts(
            set().union(*(_changed_paths(src, r) for r in new)) if new else set(), holder)
        if held:
            raise VcmError("push rejected, locked by someone else: " + describe_conflicts(held))
        plan.append((name, tip, new))
    remote_tags = _refs(dst, "tags")
    for name, rid in sorted(repo.tags().items()):  # validate everything before writing anything
        if name in remote_tags and remote_tags[name] != rid:
            raise VcmError(f"tag {name} already points at a different revision on the remote")
    for name, tip, new in plan:
        report["objects"] += _copy_revisions(src, dst, new)
        _set_ref(dst, "heads", name, tip)
        report["branches"][name] = f"updated to {tip[:10]}"
    for name, rid in sorted(repo.tags().items()):
        if name not in remote_tags:
            report["objects"] += _copy_revisions(src, dst, _closure(src, rid))
            _set_ref(dst, "tags", name, rid)
            report["tags"][name] = rid[:10]
    report["records"] = _sync_records(src, dst)
    return report


def pull(repo: Repository, remote: str, branch: str | None = None, force: bool = False) -> dict:
    """Fetch objects and records, then fast-forward local branches to the remote's."""
    src_path = resolve_remote(repo, remote)
    if not src_path.is_dir():
        raise VcmError(f"remote not found: {src_path}")
    src, dst = _side(src_path), _repo_side(repo)
    heads = _refs(src, "heads")
    if branch:
        if branch not in heads:
            raise VcmError(f"remote has no branch {branch}")
        heads = {branch: heads[branch]}
    report = {"remote": str(src_path), "branches": {}, "tags": {}, "objects": 0, "records": 0}
    for name, tip in sorted(heads.items()):
        known = set(_closure(dst, repo.branches()[name])) if name in repo.branches() else set()
        report["objects"] += _copy_revisions(src, dst, _closure(src, tip, stop=known))
        local = repo.branches().get(name)
        if local == tip:
            report["branches"][name] = "up to date"
        elif force and local and local not in _closure(dst, tip):
            repo._set_ref("heads", name, tip)
            report["branches"][name] = f"force-reset to {tip[:10]}"
        else:
            repo.fast_forward(name, tip)
            report["branches"][name] = f"fast-forwarded to {tip[:10]}"
    local_tags = repo.tags()
    for name, rid in sorted(_refs(src, "tags").items()):
        if name in local_tags and local_tags[name] != rid:
            raise VcmError(f"tag {name} already points at a different revision locally")
        if name not in local_tags:
            report["objects"] += _copy_revisions(src, dst, _closure(src, rid))
            repo._set_ref("tags", name, rid)
            report["tags"][name] = rid[:10]
    report["records"] = _sync_records(src, dst)
    return report


def clone(remote: str, dest: str | Path) -> Repository:
    """New repository from a remote, with ``origin`` configured and the default branch checked out."""
    src_path = remote_path(remote)
    if not src_path.is_dir():
        raise VcmError(f"remote not found: {src_path}")
    heads = _refs(_side(src_path), "heads")
    if not heads:
        raise VcmError("remote has no branches to clone")
    branch = "main" if "main" in heads else sorted(heads)[0]
    repo = Repository.init(dest, branch=branch)
    add_remote(repo, "origin", str(src_path))
    pull(repo, "origin")
    return repo
