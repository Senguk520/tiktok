"""HTTP security defaults shared by both local FastAPI services."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class LoopbackOnlyMiddleware(BaseHTTPMiddleware):
    """Reject Collector HTTP traffic that did not originate from loopback."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.client.host if request.client is not None else ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Starlette's in-process TestClient uses this synthetic hostname.
            if host != "testclient":
                return JSONResponse({"detail": "collector is loopback only"}, status_code=403)
        else:
            if not address.is_loopback:
                return JSONResponse({"detail": "collector is loopback only"}, status_code=403)
        return await call_next(request)


class NoStoreSecurityMiddleware(BaseHTTPMiddleware):
    """Apply no-store and browser hardening to every response."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if "etag" in response.headers:
            del response.headers["etag"]
        return response


def _frontend_origins() -> list[str]:
    configured = os.environ.get(
        "FRONTEND_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    )
    origins = [origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()]
    if not origins or any(origin == "*" for origin in origins):
        raise ValueError("FRONTEND_ORIGINS must contain explicit origins")
    return origins


def install_security_middleware(app: FastAPI, *, allow_browser: bool = False) -> None:
    """Install deterministic no-cache headers and optional narrow browser CORS."""

    add_middleware = app.add_middleware
    if allow_browser:
        add_middleware(
            CORSMiddleware,
            allow_origins=_frontend_origins(),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Accept", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
            expose_headers=["X-Request-ID"],
            max_age=0,
        )
    else:
        add_middleware(LoopbackOnlyMiddleware)
    # Added last so even CORS preflight and loopback denials receive no-store.
    add_middleware(NoStoreSecurityMiddleware)
