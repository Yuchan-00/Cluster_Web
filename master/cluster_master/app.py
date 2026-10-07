"""Build the four listener apps around one shared `AppState` (security.md 4.1).

web      127.0.0.1:8000   /api/*, /ws/ui                 users (tailscale serve in front)
agent    127.0.0.1:8001   /ws/agent                      node tokens (Caddy TLS in front)
internal internal.sock    /internal/api/*                service tokens
admin    admin.sock       /internal/admin/*              root (socket mode 0600)

Each app only contains its own routes, so a request on the wrong listener is a 404 before any
handler runs. OpenAPI/docs are off everywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from .api.internal import admin_router, internal_router
from .api.web import router as web_router
from .auth.authz import check_routes, node_token
from .auth.resolvers import (
    AdminSocketResolver,
    DenyAllResolver,
    DevAdminResolver,
    ServiceTokenResolver,
)
from .state import AppState

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@dataclass
class Apps:
    web: FastAPI
    agent: FastAPI
    internal: FastAPI
    admin: FastAPI

    def all(self) -> list[tuple[str, FastAPI]]:
        return [
            ("web", self.web),
            ("agent", self.agent),
            ("internal", self.internal),
            ("admin", self.admin),
        ]


def _app(name: str, state: AppState, resolver: Any) -> FastAPI:
    app = FastAPI(
        title=f"cluster-master {name}",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.cluster = state
    app.state.resolve_principal = resolver
    app.state.listener = name

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Never leak a traceback to a client; the log has it.
        log.exception("%s: unhandled error on %s %s", name, request.method, request.url.path)
        # this handler runs outside the http middleware stack, so add the headers here too
        return JSONResponse({"detail": "internal error"}, status_code=500, headers=SECURITY_HEADERS)

    return app


def build_apps(state: AppState) -> Apps:
    cfg = state.config
    web_resolver = DevAdminResolver() if cfg.dev.unauthenticated_admin else DenyAllResolver()
    web = _app("web", state, web_resolver)
    web.include_router(web_router)

    agent = _app("agent", state, None)

    @agent.websocket("/ws/agent", dependencies=[node_token()])
    async def ws_agent(ws: WebSocket) -> None:
        await state.hub.handle(ws)

    internal = _app("internal", state, ServiceTokenResolver(state.service_tokens))
    internal.include_router(internal_router)

    admin = _app("admin", state, AdminSocketResolver())
    admin.include_router(admin_router)

    apps = Apps(web, agent, internal, admin)
    for name, app in apps.all():
        check_routes(app, name)
    return apps


__all__ = ["Apps", "build_apps"]
