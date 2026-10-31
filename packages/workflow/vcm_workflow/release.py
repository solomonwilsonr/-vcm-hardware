"""Release manifests pinning artifact hashes, revision and toolchain; verification re-checks them."""
from __future__ import annotations

import json
import os
import platform
import re
import sys
from pathlib import Path

import vcm_core
from vcm_core import Repository, VcmError, canonical_json, now_iso, sha256_bytes, sha256_file
from vcm_core.signing import add_signature, sign_digest, verify_signature
from vcm_validation import validate_revision
from vcm_validation.kicad_cli import kicad_version

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SIG_PURPOSE = "release"
# Excluded from the hash so that signing a release does not change the digest being signed.
UNHASHED = ("manifest_sha256", "signatures")


def to_yaml(obj, indent: int = 0) -> str:
    """Minimal YAML emitter (block style; JSON-quoted scalars) for manifest output."""
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n"
        out = ""
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out += f"{pad}{json.dumps(k)}:\n{to_yaml(v, indent + 1)}"
            else:
                out += f"{pad}{json.dumps(k)}: {_scalar(v)}\n"
        return out
    if isinstance(obj, list):
        if not obj:
            return pad + "[]\n"
        out = ""
        for v in obj:
            if isinstance(v, (dict, list)) and v:
                out += f"{pad}-\n{to_yaml(v, indent + 1)}"
            else:
                out += f"{pad}- {_scalar(v)}\n"
        return out
    return pad + _scalar(obj) + "\n"


def _scalar(v) -> str:
    if isinstance(v, dict):
        return "{}"
    if isinstance(v, list):
        return "[]"
    return json.dumps(v)


def _manifest_hash(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k not in UNHASHED}
    return sha256_bytes(canonical_json(body))


def create_release(repo: Repository, name: str, rev: str = "HEAD", out: str | Path | None = None,
                   run_kicad: bool = False) -> dict:
    if not NAME_RE.match(name):
        raise VcmError(f"invalid release name: {name!r}")
    if repo.record_exists("releases", name):
        raise VcmError(f"release already exists: {name}")
    revision = repo.get_revision(rev)
    val = validate_revision(repo, revision.id, run_kicad=run_kicad, attach=True)
    manifest = {
        "schema": "vcm-release/1", "name": name, "created_at": now_iso(),
        "revision": revision.id, "parents": revision.parents,
        "revision_message": revision.message,
        "artifacts": {p: {"hash": a.hash, "size": a.size, "category": a.category}
                      for p, a in sorted(revision.artifacts.items())},
        "toolchain": {"vcm": vcm_core.__version__, "python": sys.version.split()[0],
                      "platform": platform.platform(), "kicad_cli": kicad_version() or "not installed"},
        "validation": {"status": val["status"], "counts": val["counts"],
                       "evidence_hash": val["_evidence"]["hash"],
                       "kicad_cli": (val["kicad_cli"] or {}).get("status", "not run")},
    }
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    repo.save_record("releases", name, manifest)
    if out:
        out = Path(out)
        out.write_text(to_yaml(manifest) if out.suffix in (".yaml", ".yml")
                       else json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def load_manifest(repo: Repository, name_or_path: str) -> dict:
    if os.path.isfile(name_or_path):
        text = Path(name_or_path).read_text()
        if name_or_path.endswith((".yaml", ".yml")):
            import yaml  # optional dependency, only for reading YAML manifests
            return yaml.safe_load(text)
        return json.loads(text)
    return repo.load_record("releases", name_or_path)


def sign_release(repo: Repository, name: str, key_name: str) -> dict:
    """Attach a detached Ed25519 signature over ``manifest_sha256`` to a stored release."""
    m = repo.load_record("releases", name)
    if m.get("manifest_sha256") != _manifest_hash(m):
        raise VcmError(f"release {name} fails its own hash check; refusing to sign it")
    add_signature(m, sign_digest(key_name, m["manifest_sha256"], SIG_PURPOSE))
    repo.save_record("releases", name, m)
    return m


def verify_release(repo: Repository, name_or_path: str, worktree: bool = False,
                   require_signature: bool = False) -> dict:
    """Re-check manifest self-hash, revision object, every artifact blob and (optionally) the working tree.

    Signatures are checked against ``trustdb.json``. An unsigned release still verifies unless
    ``require_signature`` is set, because hashes alone prove consistency, not authenticity; the
    result's ``authenticated`` field says whether a trusted key actually vouched for it.
    """
    m = load_manifest(repo, name_or_path)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    check("manifest_hash", m.get("manifest_sha256") == _manifest_hash(m), "manifest content matches its recorded hash")
    rid = m["revision"]
    rev = None
    if repo.store.verify(rid):
        rev = repo.get_revision(rid)
        check("revision_object", True, rid[:10])
    else:
        check("revision_object", False, f"revision object {rid[:10]} missing or corrupted")
    if rev is not None:
        pinned = {p: a["hash"] for p, a in m["artifacts"].items()}
        actual = {p: a.hash for p, a in rev.artifacts.items()}
        check("artifact_set", pinned == actual, "manifest artifacts equal revision snapshot")
    for path, a in sorted(m["artifacts"].items()):
        ok = repo.store.verify(a["hash"])
        check(f"artifact:{path}", ok, "" if ok else "blob missing or hash mismatch")
    if worktree:
        for path, a in sorted(m["artifacts"].items()):
            f = repo.root / path
            ok = f.is_file() and sha256_file(f) == a["hash"]
            check(f"worktree:{path}", ok, "" if ok else "working file differs from release")
    signers = [verify_signature(sig, m.get("manifest_sha256", ""), SIG_PURPOSE, repo.root)
               for sig in m.get("signatures", [])]
    for sg in signers:
        check(f"signature:{sg['key_id']}", sg["valid"] and sg["trusted"],
              f"{sg['name']}: " + (sg["detail"] or "valid signature from a trusted key"))
    authenticated = any(sg["valid"] and sg["trusted"] for sg in signers)
    if require_signature:
        check("signature_required", authenticated, "a trusted key signed this release"
              if authenticated else "no valid signature from a key in trustdb.json")
    problems = [c for c in checks if not c["ok"]]
    return {"release": m["name"], "revision": rid, "ok": not problems, "checks": checks,
            "problems": problems, "authenticated": authenticated,
            "signers": [{"key_id": s_["key_id"], "name": s_["name"], "valid": s_["valid"],
                         "trusted": s_["trusted"]} for s_ in signers]}
