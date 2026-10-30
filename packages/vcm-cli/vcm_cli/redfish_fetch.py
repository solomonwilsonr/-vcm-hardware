"""Live Redfish capture. The only part of VCM that talks to a machine, and only when asked.

Walks the service from ``/redfish/v1`` along ``@odata.id`` links and saves every resource it reads
into a capture file. Parsing that file is then the same offline step as for any saved capture.
TLS certificates are verified by default; BMCs commonly use self-signed certificates, so pass a CA
file rather than disabling verification wherever possible.
"""
from __future__ import annotations

import base64
import json
import ssl
import urllib.request
from collections import deque

#: collections worth following; everything else under the service root is skipped
FOLLOW = ("Systems", "Chassis", "Managers", "Processors", "Memory", "Power", "PowerSubsystem",
          "PowerSupplies", "EnvironmentMetrics", "NetworkAdapters", "Storage", "Drives",
          "UpdateService", "FirmwareInventory", "Members")


def _links(body, out: list[str]) -> None:
    if isinstance(body, dict):
        for k, v in body.items():
            if k == "@odata.id" and isinstance(v, str):
                out.append(v)
            elif k in FOLLOW or k.isdigit() or isinstance(v, list):
                _links(v, out)
    elif isinstance(body, list):
        for v in body:
            _links(v, out)


def fetch(base_url: str, user: str, password: str, ca_file: str | None = None,
          verify_tls: bool = True, limit: int = 2000, timeout: float = 20.0) -> dict:
    ctx = ssl.create_default_context(cafile=ca_file)
    if not verify_tls:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    base = base_url.rstrip("/")
    seen: dict[str, dict] = {}
    queue = deque(["/redfish/v1"])
    while queue and len(seen) < limit:
        uri = queue.popleft().split("#", 1)[0].rstrip("/")
        if uri in seen or not uri.startswith("/redfish/v1"):
            continue
        req = urllib.request.Request(base + uri, headers={"Authorization": auth,
                                                          "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:  # noqa: S310
                body = json.loads(r.read())
        except (OSError, ValueError) as exc:
            seen[uri] = {"_error": str(exc)}
            continue
        seen[uri] = body
        found: list[str] = []
        _links(body, found)
        queue.extend(found)
    return {"schema": "vcm-redfish-capture/1", "source": base, "resources": seen}
