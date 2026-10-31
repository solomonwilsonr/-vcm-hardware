"""``python -m vcm_api``: serve the API. Binds to localhost and requires a token by default."""
from __future__ import annotations

import argparse
import secrets
import sys
import tempfile
import webbrowser

from vcm_core import VcmError

from .app import create_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m vcm_api", description=__doc__)
    ap.add_argument("--repo", help="repository path (default: $VCM_REPO or the current directory)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true",
                    help="build a sample GPU-node workspace and serve it with a one-time token")
    ap.add_argument("--open", action="store_true", help="with --demo: open the browser, signed in")
    ap.add_argument("--insecure", action="store_true",
                    help="serve WITHOUT authentication (identities become self-asserted)")
    args = ap.parse_args(argv)
    if args.demo:
        return _demo(args)
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


def _demo(args) -> int:
    """Seed the demo workspace and serve it. The token is random, shown once, and passed to the
    browser in the URL *fragment*, which is never sent to any server."""
    import uvicorn

    from .demo import build_demo
    dest = args.repo or tempfile.mkdtemp(prefix="vcm-demo-")
    try:
        build_demo(dest)
    except (RuntimeError, VcmError) as e:
        print(f"vcm-api: could not build the demo: {e}", file=sys.stderr)
        return 2
    token = secrets.token_urlsafe(24)
    url = f"http://{args.host}:{args.port}/#token={token}"
    print(f"\n  VCM demo ready ({dest})\n\n  Open:  {url}\n\n  You are signed in as 'you' "
          "(maintainer). Ctrl+C to stop.\n")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(create_app(dest, tokens={token: ("you", "maintainer")}), host=args.host,
                port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
