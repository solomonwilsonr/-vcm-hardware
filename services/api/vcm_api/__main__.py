"""``python -m vcm_api``: serve the API. Binds to localhost and requires a token by default."""
from __future__ import annotations

import argparse
import sys

from vcm_core import VcmError

from .app import create_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vcm_api", description=__doc__)
    ap.add_argument("--repo", help="repository path (default: $VCM_REPO or the current directory)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--insecure", action="store_true",
                    help="serve WITHOUT authentication (identities become self-asserted)")
    args = ap.parse_args(argv)
    try:
        app = create_app(args.repo, insecure=args.insecure)
    except VcmError as e:
        print(f"vcm-api: {e}", file=sys.stderr)
        return 2
    if args.insecure:
        print("vcm-api: WARNING: running without authentication", file=sys.stderr)
    try:
        import uvicorn
    except ImportError:
        print("vcm-api: uvicorn is not installed (pip install 'vcm-hardware[api]')", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
