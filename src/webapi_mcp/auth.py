"""
Per-request context: capture the X-WebAPI-Key header from the inbound MCP
request and stash it in a ContextVar so tool handlers can read it without the
MCP SDK having to know about HTTP.
"""
from contextvars import ContextVar
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import settings

_api_key_ctx: ContextVar[str | None] = ContextVar("webapi_key", default=None)


def current_api_key() -> str | None:
    return _api_key_ctx.get()


class ApiKeyMiddleware:
    """
    Pure ASGI middleware (no BaseHTTPMiddleware) so streaming / SSE responses
    are not buffered.

    Reads:
      - X-WebAPI-Key     : the user's personal WebAPI API key (required for /mcp)
      - X-Gateway-Secret : optional shared secret for an extra perimeter check
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Let health checks through unauthenticated
        if path in ("/healthz", "/readyz"):
            await self.app(scope, receive, send)
            return

        # Build a lightweight header lookup
        headers = {
            k.decode(): v.decode()
            for k, v in scope.get("headers", [])
        }

        if settings.shared_gateway_secret:
            if headers.get("x-gateway-secret") != settings.shared_gateway_secret:
                response = JSONResponse(
                    {"error": "gateway secret missing or invalid"}, status_code=401
                )
                await response(scope, receive, send)
                return

        key = headers.get("x-webapi-key")
        token = _api_key_ctx.set(key)
        try:
            await self.app(scope, receive, send)
        finally:
            _api_key_ctx.reset(token)