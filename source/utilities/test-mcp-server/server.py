"""Minimal MCP server for exercising the AI gateway in development clusters.

Serves two tools and one resource over streamable HTTP at /mcp. It also has
plain HTTP routes that give gateway traffic policies something to act on.
Not for production use: it has no authentication.
"""

import asyncio
import itertools

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

mcp = FastMCP(
    "canvas-test-mcp",
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    json_response=True,
)

_flaky_calls = itertools.count(1)


@mcp.tool()
def echo(message: str) -> str:
    """Return the message unchanged."""
    return message


@mcp.tool()
def add(a: float, b: float) -> float:
    """Return the sum of two numbers."""
    return a + b


@mcp.resource("canvas://about", mime_type="text/plain")
def about() -> str:
    """Describe this server."""
    return "Test MCP server for the ODA Canvas AI gateway."


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/flaky", methods=["GET"])
async def flaky(_: Request) -> JSONResponse:
    """Fail every odd-numbered call, so a single retry succeeds."""
    call = next(_flaky_calls)
    status = 503 if call % 2 else 200
    return JSONResponse({"call": call}, status_code=status)


@mcp.custom_route("/slow", methods=["GET"])
async def slow(_: Request) -> JSONResponse:
    """Respond after 5 seconds, to trigger gateway request timeouts."""
    await asyncio.sleep(5)
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
