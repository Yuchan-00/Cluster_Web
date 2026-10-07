"""Route snapshot: every route on every listener declares who may call it, and the table matches
tests/route_snapshot.txt (a new route is a conscious, reviewed change)."""

from __future__ import annotations

import pathlib

import httpx
import pytest

from cluster_master.app import build_apps
from cluster_master.auth.authz import Requirement, check_routes, route_requirements
from cluster_master.auth.principal import Principal

SNAPSHOT = pathlib.Path(__file__).with_name("route_snapshot.txt")


def _table(apps) -> list[str]:
    lines = []
    for name, app in apps.all():
        for methods, path, req in route_requirements(app):
            lines.append(f"{name} {methods} {path} {req.describe() if req else 'NONE'}")
    return sorted(lines)


def test_every_route_declares_a_requirement(apps):
    for name, app in apps.all():
        check_routes(app, name)
    assert all(not line.endswith(" NONE") for line in _table(apps))


def test_route_snapshot(apps):
    expected = sorted(line for line in SNAPSHOT.read_text().splitlines() if line.strip())
    actual = _table(apps)
    assert actual == expected, (
        "route table changed; review it and update tests/route_snapshot.txt:\n" + "\n".join(actual)
    )


def test_listeners_are_disjoint(apps):
    seen: dict[str, str] = {}
    for name, app in apps.all():
        for _, path, _ in route_requirements(app):
            assert seen.setdefault(path, name) == name, f"{path} on two listeners"
    web_paths = {p for _, p, _ in route_requirements(apps.web)}
    assert all(p.startswith(("/api/", "/ws/ui")) for p in web_paths)
    assert all(p.startswith("/internal/api/") for _, p, _ in route_requirements(apps.internal))
    assert all(p.startswith("/internal/admin/") for _, p, _ in route_requirements(apps.admin))
    assert [p for _, p, _ in route_requirements(apps.agent)] == ["/ws/agent"]


def test_requirement_matrix():
    viewer = Principal("user", "v", "web", role="viewer")
    admin = Principal("user", "a", "web", role="admin")
    bot = Principal("service", "telegram-bot", "telegram", scopes=frozenset({"read"}))
    root = Principal("root", "root", "cli", role="admin")
    r_viewer, r_admin = Requirement(roles=("viewer",)), Requirement(roles=("admin",))
    r_read, r_root = Requirement(scopes=("read",)), Requirement(root=True)
    assert r_viewer.satisfied_by(viewer) and r_viewer.satisfied_by(admin)
    assert not r_admin.satisfied_by(viewer) and r_admin.satisfied_by(admin)
    assert not r_viewer.satisfied_by(bot) and r_read.satisfied_by(bot)
    assert not r_read.satisfied_by(admin)
    assert r_root.satisfied_by(root) and not r_root.satisfied_by(admin)
    assert r_admin.satisfied_by(root)  # root may use the user routes on the admin socket
    assert not Requirement(node=True).satisfied_by(root)


async def test_wrong_listener_is_404(web, internal, admin):
    assert (await web.get("/internal/admin/status")).status_code == 404
    assert (await web.get("/internal/api/nodes")).status_code == 404
    assert (await internal.get("/api/nodes")).status_code == 404
    assert (await admin.get("/api/nodes")).status_code == 404
    assert (await admin.get("/internal/api/nodes")).status_code == 404


async def test_docs_are_disabled(web):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await web.get(path)).status_code == 404


async def test_security_headers(web):
    r = await web.get("/api/cluster/summary")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert "server" not in r.headers


async def test_web_without_dev_admin_denies_everyone(state):
    state.config.dev.unauthenticated_admin = False
    apps = build_apps(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps.web), base_url="http://web"
    ) as client:
        r = await client.get("/api/nodes")
        assert r.status_code == 401
        assert r.headers.get("www-authenticate") == "Bearer"


async def test_dev_admin_is_loopback_only(apps):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps.web, client=("10.1.2.3", 4444)),
        base_url="http://web",
    ) as client:
        assert (await client.get("/api/nodes")).status_code == 401


async def test_admin_socket_refuses_tcp_clients(apps):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps.admin, client=("127.0.0.1", 5555)),
        base_url="http://admin",
    ) as client:
        assert (await client.get("/internal/admin/status")).status_code == 401


@pytest.mark.parametrize("path", ["/api/audit", "/api/audit/verify"])
async def test_admin_routes_need_admin(apps, path):
    # the dev resolver yields an admin; swap in a viewer to check the role gate
    async def viewer(conn):
        return Principal("user", "v", "web", role="viewer", ip="127.0.0.1")

    apps.web.state.resolve_principal = viewer
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps.web), base_url="http://web"
    ) as client:
        assert (await client.get(path)).status_code == 403
        assert (await client.get("/api/nodes")).status_code == 200
        assert (
            await client.post("/api/nodes", json={"id": "x", "board": "rpi3"})
        ).status_code == 403
