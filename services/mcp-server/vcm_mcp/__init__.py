"""MCP server and proposal workflow: lets an agent read VCM and ask a human for changes."""
from .proposals import approve, get_proposal, list_proposals, propose, reject
from .server import handle, serve

__all__ = ["approve", "get_proposal", "list_proposals", "propose", "reject", "handle", "serve"]
