"""Physical traceability: device registry, passports, and affected-device lookup."""
from __future__ import annotations

import os
import re

from vcm_core import Repository, VcmError, now_iso
from vcm_diff import diff_revisions
from vcm_parsers import design_from_revision

from .qr import qr_svg

SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def base_url() -> str:
    return os.environ.get("VCM_BASE_URL", "https://vcm.example.com/device").rstrip("/")


def register_device(repo: Repository, serial: str, release: str, batch: str = "",
                    notes: str = "") -> dict:
    if not SERIAL_RE.match(serial):
        raise VcmError(f"invalid serial: {serial!r}")
    rel = repo.load_record("releases", release)  # raises if the release does not exist
    if (repo.dir / "devices" / f"{serial}.json").exists():
        raise VcmError(f"device already registered: {serial}")
    payload = f"{base_url()}/{serial}"
    record = {"serial": serial, "release": release, "revision": rel["revision"], "batch": batch,
              "notes": notes, "registered_at": now_iso(), "qr_payload": payload}
    repo.save_record("devices", serial, record)
    svg = repo.dir / "devices" / f"{serial}.svg"
    svg.write_text(qr_svg(payload))
    return {**record, "qr_svg": str(svg)}


def list_devices(repo: Repository) -> list[dict]:
    return repo.list_records("devices")


def passport(repo: Repository, serial: str) -> dict:
    dev = repo.load_record("devices", serial)
    rel = repo.load_record("releases", dev["release"])
    rev = repo.get_revision(dev["revision"])
    design = design_from_revision(repo, rev)
    lineage = [{"id": r.id, "message": r.message, "timestamp": r.timestamp}
               for r in reversed(repo.log(rev.id))]
    tags = sorted(t for t, rid in repo.tags().items() if rid == rev.id)
    validation = repo.latest_evidence(rev.id, "validation")
    return {
        "serial": serial, "batch": dev["batch"], "registered_at": dev["registered_at"],
        "notes": dev["notes"],
        "release": {"name": rel["name"], "manifest_sha256": rel.get("manifest_sha256"),
                    "created_at": rel.get("created_at")},
        "revision": {"id": rev.id, "message": rev.message, "author": rev.author,
                     "timestamp": rev.timestamp, "tags": tags},
        "lineage": lineage,
        "bom": [{"ref": c.ref, "value": c.value, "mpn": c.mpn, "footprint": c.footprint}
                for c in design.components.values()],
        "firmware": [{"path": a.path, "hash": a.hash} for a in rev.by_category("firmware")],
        "validation": ({"status": validation["status"], "counts": validation["counts"]}
                       if validation else None),
        "qr": {"payload": dev["qr_payload"], "svg": qr_svg(dev["qr_payload"])},
    }


def affected_by_component(repo: Repository, ref: str, value: str | None = None) -> list[dict]:
    """Devices whose build includes component ``ref`` (optionally only with this value or MPN)."""
    cache: dict[str, object] = {}
    out = []
    for dev in list_devices(repo):
        design = cache.get(dev["revision"]) or design_from_revision(repo, dev["revision"])
        cache[dev["revision"]] = design
        comp = design.components.get(ref)
        if comp and (value is None or value in (comp.value, comp.mpn)):
            out.append({"serial": dev["serial"], "release": dev["release"], "batch": dev["batch"],
                        "revision": dev["revision"], "component": ref, "value": comp.value,
                        "mpn": comp.mpn, "footprint": comp.footprint})
    return out


def affected_by_revision(repo: Repository, rev: str) -> dict:
    """Devices built from ``rev`` or a descendant, i.e. carrying the changes ``rev`` introduced."""
    rid = repo.resolve(rev)
    revision = repo.get_revision(rid)
    if revision.parents:
        changed = diff_revisions(repo, revision.parents[0], rid)["changed_refs"]
    else:
        changed = sorted(design_from_revision(repo, revision).components)
    devices = [{"serial": d["serial"], "release": d["release"], "batch": d["batch"],
                "revision": d["revision"]}
               for d in list_devices(repo) if repo.is_ancestor(rid, d["revision"])]
    return {"revision": rid, "changed_components": changed, "devices": devices}
