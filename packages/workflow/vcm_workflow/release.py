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
from vcm_core.signing import load_secret, load_trust, sign_digest, verify_signature
from vcm_validation import validate_revision
from vcm_validation.kicad_cli import kicad_version

from .policy import evaluate, load_policy, observed

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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


#: fields outside the signed body: the hash itself, and the detached signatures over that hash
_UNHASHED = ("manifest_sha256", "signatures")
SIGN_PURPOSE = "release"


def _manifest_hash(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k not in _UNHASHED}
    return sha256_bytes(canonical_json(body))


def release_policy(repo: Repository, revision) -> dict:
    """Policy at the release stage, judged on the change from the revision's first parent."""
    pol = load_policy(repo, revision.id)
    base = revision.parents[0] if revision.parents else None
    report = None
    if pol and base:
        from vcm_ai import analyze  # only paid for when a policy exists
        report = analyze(repo, base, revision.id)
    return evaluate(pol, report, "release", observed_base=observed(repo, base))


def create_release(repo: Repository, name: str, rev: str = "HEAD", out: str | Path | None = None,
                   run_kicad: bool = False, override_policy: str | None = None,
                   overridden_by: str | None = None) -> dict:
    """Pin a revision. Refused when the committed policy is violated, unless the override reason
    is given; an override is recorded in the manifest and the audit log, never silent."""
    if not NAME_RE.match(name):
        raise VcmError(f"invalid release name: {name!r}")
    if (repo.dir / "releases" / f"{name}.json").exists():
        raise VcmError(f"release already exists: {name}")
    revision = repo.get_revision(rev)
    pol = release_policy(repo, revision)
    if not pol["ok"] and not override_policy:
        raise VcmError("release blocked by vcm-policy.json: " + "; ".join(
            f"{v['id']}: {', '.join(v['problems'])}" for v in pol["violations"])
            + " (pass an override reason to release anyway; it is recorded)")
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
        # anchors the audit chain: rewriting history before this point breaks this release
        "audit_head": repo.audit.head(),
        "policy": {"present": pol["policy"], "ok": pol["ok"],
                   "violations": [v["id"] for v in pol["violations"]],
                   "skipped": [x["id"] for x in pol["skipped"]]},
    }
    if not pol["ok"]:
        manifest["policy"]["override"] = {"reason": override_policy,
                                          "by": overridden_by or os.environ.get("VCM_AUTHOR",
                                                                                "unknown")}
    manifest["manifest_sha256"] = _manifest_hash(manifest)
    repo.save_record("releases", name, manifest)
    repo.audit.append("release.create", name, {"revision": revision.id,
                                               "manifest_sha256": manifest["manifest_sha256"]})
    if not pol["ok"]:
        repo.audit.append("policy.override", name, manifest["policy"])
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


def sign_release(repo: Repository, name: str, key: str, signer: str | None = None) -> dict:
    """Add a detached Ed25519 signature over the release's manifest hash."""
    m = repo.load_record("releases", name)
    if m.get("manifest_sha256") != _manifest_hash(m):
        raise VcmError(f"release {name} does not match its own hash; refusing to sign it")
    sig = sign_digest(load_secret(key), signer or key, SIGN_PURPOSE, m["manifest_sha256"],
                      now_iso())
    trust = load_trust(repo.root)
    ok, why = verify_signature(trust, sig, SIGN_PURPOSE, m["manifest_sha256"])
    if not ok:
        raise VcmError(f"cannot sign: {why} (add it with `vcm key trust`)")
    sigs = [s for s in m.get("signatures", []) if s["key_id"] != sig["key_id"]]
    m["signatures"] = sigs + [sig]
    repo.save_record("releases", name, m)
    repo.audit.append("release.sign", name, {"key_id": sig["key_id"],
                                             "manifest_sha256": m["manifest_sha256"]},
                      actor=sig["signer"])
    return sig


def verify_release(repo: Repository, name_or_path: str, worktree: bool = False,
                   require_signature: bool = False) -> dict:
    """Re-check manifest self-hash, signatures, revision object, every artifact blob and
    (optionally) the working tree.

    Hashes prove consistency; only a signature by a trusted key proves who vouched for the release.
    """
    m = load_manifest(repo, name_or_path)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    digest_ok = m.get("manifest_sha256") == _manifest_hash(m)
    check("manifest_hash", digest_ok, "manifest content matches its recorded hash")
    trust = load_trust(repo.root)
    sigs = m.get("signatures", [])
    valid = 0
    for sig in sigs:
        ok, why = verify_signature(trust, sig, SIGN_PURPOSE, m.get("manifest_sha256", ""))
        ok = ok and digest_ok
        valid += ok
        check(f"signature:{sig.get('key_id', '?')}", ok, why)
    if require_signature and not valid:
        check("signature_required", False,
              "no valid signature by a trusted key" if sigs else "release is unsigned")
    if repo.store.has(m["revision"]):
        pol = load_policy(repo, m["revision"])
        if pol:
            res = evaluate(pol, None, "verify", signed=valid > 0)
            for v in res["violations"]:
                check(f"policy:{v['id']}", False, "; ".join(v["problems"]))
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
    problems = [c for c in checks if not c["ok"]]
    return {"release": m["name"], "revision": rid, "ok": not problems, "checks": checks,
            "problems": problems, "signed": valid > 0, "valid_signatures": valid}
