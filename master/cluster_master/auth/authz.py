"""Default-deny authorization for every route (docs/design/security.md 7.3).

Each listener app installs a `Resolver` that turns a connection into a `Principal` (or
refuses). Routes declare what they accept with `require(...)`; `check_routes()` runs at startup
and fails if any route lacks such a declaration, so a forgotten dependency is a crash, not an
open endpoint.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocketException
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.requests import HTTPConnection

from .principal import Principal

log = logging.getLogger(__name__)

Resolver = Callable[[HTTPConnection], Awaitable[Principal]]
WS_UNAUTHORIZED = 4401
WS_FORBIDDEN = 4403


class AuthError(Exception):
    """Raised by resolvers; turned into 401 (HTTP) or 4401 (WebSocket)."""

    def __init__(self, detail: str = "authentication required") -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Requirement:
    """What a route accepts. Empty tuples mean 'none of that kind'."""

    roles: tuple[str, ...] = ()  # minimum user role, e.g. ("viewer",)
    scopes: tuple[str, ...] = ()  # any of these service scopes
    root: bool = False  # the admin socket
    node: bool = False  # agent token, checked inside the endpoint (agent hub)

    def satisfied_by(self, p: Principal) -> bool:
        if p.kind == "root":
            return self.root or bool(self.roles)  # root is an admin user as well
        if p.kind == "user":
            return any(p.has_role(r) for r in self.roles)
        if p.kind == "service":
            return any(p.has_scope(s) for s in self.scopes)
        return False

    def describe(self) -> str:
        parts = []
        if self.roles:
            parts.append("role>=" + "/".join(self.roles))
        if self.scopes:
            parts.append("scope:" + "/".join(self.scopes))
        if self.root:
            parts.append("root")
        if self.node:
            parts.append("node-token")
        return " | ".join(parts) or "nobody"


def _deny(conn: HTTPConnection, status: int, detail: str) -> Exception:
    if conn.scope["type"] == "websocket":
        return WebSocketException(code=WS_UNAUTHORIZED if status == 401 else WS_FORBIDDEN)
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    return HTTPException(status_code=status, detail=detail, headers=headers)


async def resolve(conn: HTTPConnection) -> Principal:
    resolver: Resolver | None = getattr(conn.app.state, "resolve_principal", None)
    if resolver is None:
        raise _deny(conn, 401, "no authentication configured for this listener")
    try:
        principal = await resolver(conn)
    except AuthError as exc:
        raise _deny(conn, 401, exc.detail) from None
    conn.state.principal = principal
    return principal


def require(*roles: str, scopes: tuple[str, ...] = (), root: bool = False) -> Any:
    """FastAPI dependency: the caller must satisfy the requirement; yields the Principal."""
    req = Requirement(roles=tuple(roles), scopes=tuple(scopes), root=root)

    async def dependency(conn: HTTPConnection) -> Principal:
        principal = await resolve(conn)
        if not req.satisfied_by(principal):
            log.warning(
                "forbidden: %s -> %s %s (needs %s)",
                principal.describe(),
                conn.scope.get("method", "WS"),
                conn.scope.get("path"),
                req.describe(),
            )
            raise _deny(conn, 403, "forbidden")
        return principal

    dependency._cluster_requirement = req  # type: ignore[attr-defined]
    return Depends(dependency)


def node_token() -> Any:
    """Marker for the agent WebSocket: the hub validates the token itself (it needs to send a
    401 denial response and the 44xx close codes)."""
    req = Requirement(node=True)

    async def dependency() -> None:
        return None

    dependency._cluster_requirement = req  # type: ignore[attr-defined]
    return Depends(dependency)


def iter_routes(routes: Iterable[Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Walk an app's routes, including routers that FastAPI includes lazily, with prefixes."""
    for route in routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            yield prefix + route.path, route
            continue
        inner = getattr(route, "original_router", None)  # FastAPI >= 0.120 include_router
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            yield from iter_routes(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            continue
        children = getattr(route, "routes", None)  # Mount / Router
        if children is not None:
            yield from iter_routes(children, prefix + (getattr(route, "path", "") or ""))


def route_requirements(app: FastAPI) -> list[tuple[str, str, Requirement | None]]:
    """(methods, path, requirement) for every route; None when a route declares nothing."""
    out = []
    for path, route in iter_routes(app.routes):
        methods = ",".join(sorted(route.methods)) if isinstance(route, APIRoute) else "WS"
        req = None
        for dep in route.dependant.dependencies:
            found = getattr(dep.call, "_cluster_requirement", None)
            if found is not None:
                req = found
                break
        out.append((methods, path, req))
    return out


def check_routes(app: FastAPI, name: str) -> None:
    missing = [f"{m} {p}" for m, p, req in route_requirements(app) if req is None]
    if missing:
        raise RuntimeError(f"{name}: routes without an authorization requirement: {missing}")


__all__ = [
    "WS_FORBIDDEN",
    "WS_UNAUTHORIZED",
    "AuthError",
    "Requirement",
    "Resolver",
    "check_routes",
    "iter_routes",
    "node_token",
    "require",
    "resolve",
    "route_requirements",
]
