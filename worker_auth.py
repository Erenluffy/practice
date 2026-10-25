"""Shared-secret gate for worker services that are publicly reachable.

The dispatcher is the only intended caller.  Render exposes each worker URL, so
without this check a caller could bypass the dispatcher's queue and shared
per-IP limits by talking to a worker directly.  ``/api/health`` stays public for
Render's health check.
"""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

PUBLIC_PATH = "/api/health"


class DispatcherAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token.encode("utf-8")

    async def dispatch(self, request, call_next):
        # An empty token means the operator explicitly enabled the local-only
        # ALLOW_UNAUTHENTICATED_WORKER bypass.
        if not self._token:
            return await call_next(request)
        if request.url.path.rstrip("/") == PUBLIC_PATH:
            return await call_next(request)
        provided = request.headers.get(
            "x-dispatcher-token", "").encode("utf-8", "ignore")
        if not secrets.compare_digest(provided, self._token):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized worker request"},
            )
        return await call_next(request)
