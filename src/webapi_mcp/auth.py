"""
Per-request context: capture the X-WebAPI-Key header from the inbound MCP
request and stash it in a ContextVar so tool handlers can read it without the
MCP SDK having to know about HTTP.
"""
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import settings

_api_key_ctx: ContextVar[str | None] = ContextVar("webapi_key", default=None)


def current_api_key() -> str | None:
    return _api_key_ctx.get()


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """
    Reads:
      - X-WebAPI-Key  : the user's personal WebAPI API key (required)
      - X-Gateway-Secret : optional shared secret for an extra perimeter check
    """

    async def dispatch(self, request: Request, call_next):
        # Let health checks through unauthenticated
        if request.url.path in ("/healthz", "/readyz"):
            return await call_next(request)

        if settings.shared_gateway_secret:
            if request.headers.get("X-Gateway-Secret") != settings.shared_gateway_secret:
                return JSONResponse(
                    {"error": "gateway secret missing or invalid"}, status_code=401
                )

        key = request.headers.get("X-WebAPI-Key")
        token = _api_key_ctx.set(key)
        try:
            return await call_next(request)
        finally:
            _api_key_ctx.reset(token)