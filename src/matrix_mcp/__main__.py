"""Entry point.

Transports:
  - http (default): streamable-http behind static bearer auth.
      MCP_BEARER_TOKEN is required; HOST defaults to 127.0.0.1 (the Docker
      image sets HOST=0.0.0.0); PORT defaults to 3300.
  - stdio: for local single-user MCP clients (Claude Desktop / Claude Code).
      Select with `python -m matrix_mcp --stdio` or MCP_TRANSPORT=stdio.
      No bearer token needed.
"""

from __future__ import annotations

import logging
import os
import sys


def main() -> None:
    from matrix_mcp.client import TenantRegistry
    from matrix_mcp.server import build_mcp, create_app

    transport = os.environ.get("MCP_TRANSPORT", "http").lower()
    if "--stdio" in sys.argv[1:]:
        transport = "stdio"

    registry = TenantRegistry()
    primary = os.environ.get("MATRIX_PRIMARY_TENANT") or None

    if transport == "stdio":
        # Log to stderr only — stdout is the MCP transport.
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
        # httpx logs full request URLs (which contain room IDs) at INFO;
        # keep those out of the logs.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        build_mcp(registry, primary_tenant=primary).run(transport="stdio")
        return

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bearer = os.environ.get("MCP_BEARER_TOKEN", "")
    app = create_app(registry, bearer_token=bearer, primary_tenant=primary)

    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "3300"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
