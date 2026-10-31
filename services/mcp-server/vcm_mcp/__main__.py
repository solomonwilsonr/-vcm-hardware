import argparse
import sys

from .server import serve

p = argparse.ArgumentParser(prog="python -m vcm_mcp", description="VCM MCP server (stdio)")
p.add_argument("--repo", default=".", help="VCM repository (default: current directory)")
sys.exit(serve(p.parse_args().repo))
