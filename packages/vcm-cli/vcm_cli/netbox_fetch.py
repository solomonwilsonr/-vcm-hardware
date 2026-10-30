"""Live NetBox read. Only runs when ``vcm fleet netbox --url`` is invoked; read-only GETs."""
from __future__ import annotations

import json
import urllib.request


def fetch_all(base_url: str, path: str, token: str, timeout: float = 30.0,
              max_pages: int = 200) -> list[dict]:
    url = base_url.rstrip("/") + path + ("&" if "?" in path else "?") + "limit=1000"
    out: list[dict] = []
    for _ in range(max_pages):
        req = urllib.request.Request(url, headers={"Authorization": f"Token {token}",
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            page = json.loads(r.read())
        out += page.get("results", [])
        url = page.get("next")
        if not url:
            break
    return out
