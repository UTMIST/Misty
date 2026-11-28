"""The MCP endpoint at /mcp, kept separate from the /v1 routers.

Mounted only when MCP_ENABLED is true; while off, /mcp does not exist and
answers 404. This module adds no tools and no sign-in yet: every request is
rejected with 401 until #440 replaces `_NoTokensYet` with real token
verification. See docs/ARCHITECTURE.md#mcp-endpoint for why the official SDK
is used and how it mounts.
"""

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

MCP_PATH = "/mcp"


class _NoTokensYet:
    """Rejects every bearer token: no tokens can be issued until #440."""

    async def verify_token(self, token: str):
        return None


def _build_server() -> MCPServer:
    return MCPServer(name="misty", title="Misty", version="0.1.0")


@asynccontextmanager
async def _run_mcp(app: FastAPI):
    # The SDK's session manager can run only once, so each app lifespan builds a
    # fresh server. Stateless: the gateway keeps no MCP session between
    # requests, so a restart drops nothing. JSON responses: tool calls return
    # one result rather than an SSE stream. (The standalone GET stream is
    # refused in _handle; json_response does not turn it off.)
    server = _build_server()
    server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # The SDK otherwise enables localhost-only Host checks, which would
        # reject Railway's public hostname. /mcp authenticates with a bearer
        # token, never a cookie, so a rebinding page has no credential to ride.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    manager = server.session_manager
    async with manager.run():
        app.state.mcp_session_manager = manager
        try:
            yield
        finally:
            app.state.mcp_session_manager = None


async def _handle(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["method"] == "GET":
        # The SDK answers GET with an SSE stream for server-initiated messages
        # even when stateless. A stateless server never sends any, so that
        # stream would sit open forever, one idle connection per request. The
        # spec lets a server without that stream answer 405.
        response = JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Method Not Allowed"},
            },
            status_code=405,
            headers={"Allow": "POST"},
        )
        await response(scope, receive, send)
        return
    await scope["app"].state.mcp_session_manager.handle_request(scope, receive, send)


# A plain ASGI endpoint (not a FastAPI route), so it accepts every HTTP method
# the transport uses. The gateway's app-level middleware (audit log, per-IP
# edge limit) still wraps it, because it sits inside the same app.
_endpoint = AuthenticationMiddleware(
    RequireAuthMiddleware(_handle, required_scopes=[]),
    backend=BearerAuthBackend(_NoTokensYet()),
)


def mount_mcp(app: FastAPI) -> None:
    """Add /mcp to the app and run the MCP session manager with its lifespan."""
    app.router.routes.append(Route(MCP_PATH, endpoint=_endpoint, include_in_schema=False))
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            state = await stack.enter_async_context(inner(app))
            await stack.enter_async_context(_run_mcp(app))
            yield state

    app.router.lifespan_context = lifespan
