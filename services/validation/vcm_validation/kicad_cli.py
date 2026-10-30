"""Optional wrapper around ``kicad-cli`` DRC / ERC. Skips gracefully when not installed."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def kicad_version() -> str | None:
    exe = shutil.which("kicad-cli")
    if not exe:
        return None
    try:
        return subprocess.run([exe, "version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _run(exe: str, args: list[str], out: Path) -> dict:
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return {"status": "error", "message": str(e)}
    if not out.exists():
        return {"status": "error", "message": (proc.stderr or proc.stdout).strip()[:500]}
    data = json.loads(out.read_text())
    violations = data.get("violations", [])
    unconnected = data.get("unconnected_items", [])
    if not violations and "sheets" in data:  # ERC report groups violations per sheet
        violations = [v for s in data["sheets"] for v in s.get("violations", [])]
    return {"status": "pass" if not violations and not unconnected else "fail",
            "violations": len(violations), "unconnected_items": len(unconnected)}


def run_kicad_checks(files: dict[str, bytes]) -> dict:
    exe = shutil.which("kicad-cli")
    if not exe:
        return {"status": "skipped", "reason": "kicad-cli not found on PATH"}
    results: dict[str, dict] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for path, data in files.items():
            low = path.lower()
            if not low.endswith((".kicad_pcb", ".kicad_sch")):
                continue
            src = Path(tmp) / Path(path).name
            src.write_bytes(data)
            out = Path(tmp) / (src.name + ".report.json")
            sub = ["pcb", "drc"] if low.endswith(".kicad_pcb") else ["sch", "erc"]
            results[path] = _run(exe, [*sub, "--format", "json", "--output", str(out), str(src)], out)
    statuses = {r["status"] for r in results.values()}
    status = "fail" if "fail" in statuses else "error" if "error" in statuses else "pass"
    return {"status": status if results else "skipped", "version": kicad_version(), "files": results}
