"""Web, internal and admin REST behaviour on the shared state."""

from __future__ import annotations

import time

import pytest

from cluster_master.models import Metrics
from cluster_master.secrets import NODE_PREFIX, SERVICE_PREFIX, is_token

from .conftest import ACTOR, metrics

NODE = {"id": "rpi3-01", "board": "rpi3", "labels": {"zone": "shelf-a"}, "capacity": {"slots": 2}}


async def test_node_lifecycle(web):
    r = await web.post("/api/nodes", json=NODE)
    assert r.status_code == 201, r.text
    created = r.json()
    token = created.pop("token")
    assert is_token(token, NODE_PREFIX)
    assert created["labels"] == {"zone": "shelf-a"} and created["capacity"] == {"slots": 2}
    assert created["has_token"] is True and created["online"] is False

    r = await web.get("/api/nodes")
    assert r.status_code == 200
    (node,) = r.json()
    assert node["id"] == "rpi3-01" and "token" not in node and node["latest"] is None
    assert token not in r.text

    r = await web.post("/api/nodes", json=NODE)
    assert r.status_code == 409

    r = await web.patch(
        "/api/nodes/rpi3-01",
        json={
            "labels": {"zone": "shelf-b", "gpu": False},
            "sched_state": "cordoned",
            "sched_reason": "test",
        },
    )
    assert r.status_code == 200
    assert r.json()["labels"] == {"zone": "shelf-b", "gpu": False}
    assert r.json()["sched_state"] == "cordoned"

    r = await web.post("/api/nodes/rpi3-01/token")
    assert r.status_code == 200
    new_token = r.json()["token"]
    assert is_token(new_token, NODE_PREFIX) and new_token != token

    r = await web.delete("/api/nodes/rpi3-01/token")
    assert r.status_code == 204
    assert (await web.get("/api/nodes/rpi3-01")).json()["has_token"] is False

    r = await web.delete("/api/nodes/rpi3-01")
    assert r.status_code == 204
    assert (await web.get("/api/nodes/rpi3-01")).status_code == 404
    assert (await web.get("/api/nodes")).json() == []

    r = await web.get("/api/audit")
    actions = [row["action"] for row in reversed(r.json())]
    assert actions == [
        "node.register",
        "node.update",
        "node.token_rotate",
        "node.token_revoke",
        "node.remove",
    ]
    row = r.json()[-1]
    assert row["actor_type"] == "user" and row["actor_id"] == "dev" and row["channel"] == "web"
    assert row["ip"] == "127.0.0.1"
    verify = (await web.get("/api/audit/verify")).json()
    assert verify["ok"] and verify["rows"] == 5


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"id": "Bad_Name", "board": "rpi3"}, "node id"),
        ({"id": "ok", "board": "esp32"}, "board"),
        ({"id": "ok", "board": "rpi3", "labels": {"Zone": "x"}}, "label key"),
        ({"id": "ok", "board": "rpi3", "labels": {"zone": ["x"]}}, "label"),
        ({"id": "ok", "board": "rpi3", "capacity": {"slots": 999}}, "capacity.slots"),
        ({"id": "ok", "board": "rpi3", "capacity": {"gpus": 1}}, "capacity"),
    ],
)
async def test_node_validation(web, body, fragment):
    r = await web.post("/api/nodes", json=body)
    assert r.status_code == 422, r.text
    assert fragment in r.text


async def test_unknown_body_field_rejected(web):
    r = await web.post("/api/nodes", json={"id": "ok", "board": "rpi3", "token": "cat_x"})
    assert r.status_code == 422


async def test_summary_status_and_lockdown(web, state):
    summary = (await web.get("/api/cluster/summary")).json()
    assert summary["nodes_total"] == 0 and summary["lockdown"]["active"] is False

    r = await web.post("/api/system/lockdown", json={"active": True, "reason": "drill"})
    assert r.status_code == 200
    assert r.json()["active"] is True and r.json()["changed"] is True
    assert state.lockdown.active
    r = await web.post("/api/system/lockdown", json={"active": True})
    assert r.json()["changed"] is False
    status = (await web.get("/api/system/status")).json()
    assert status["lockdown"]["reason"] == "drill" and status["agents"] == []
    r = await web.post("/api/system/lockdown", json={"active": False})
    assert r.json()["active"] is False
    actions = [row["action"] for row in (await web.get("/api/audit")).json()]
    assert actions[:2] == ["system.unlock", "system.lockdown"]


async def test_lockdown_survives_restart(state):
    await state.lockdown.set(True, actor=ACTOR, reason="persist")
    from cluster_master.services.lockdown import LockdownService

    fresh = LockdownService(state.db, state.audit, state.bus)  # as a restarted master would
    assert await fresh.load() is True
    assert fresh.reason == "persist" and fresh.changed_by == "tester"


async def test_metrics_endpoints(web, state):
    await web.post("/api/nodes", json=NODE)
    m = Metrics.model_validate(metrics(cpu=33.0))
    state.metrics.add("rpi3-01", m)
    r = await web.get("/api/nodes/rpi3-01/metrics")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1 and rows[0]["cpu"] == 33.0 and rows[0]["temp"] == 45.5
    node = (await web.get("/api/nodes/rpi3-01")).json()
    assert node["latest"]["data"]["extra"] == {"throttled": "0x0"}  # junk filtered
    listing = (await web.get("/api/nodes")).json()
    assert listing[0]["latest"]["cpu"] == 33.0

    assert await state.metrics.rollup(now=time.time()) == 1
    r = await web.get("/api/nodes/rpi3-01/metrics", params={"history": "true"})
    rows = r.json()["rows"]
    assert len(rows) == 1 and rows[0]["cpu_avg"] == 33.0 and rows[0]["extra"] == {"samples": 1}
    assert (await web.get("/api/nodes/nope/metrics")).status_code == 404
    assert (await web.get("/api/nodes/rpi3-01/metrics", params={"limit": 0})).status_code == 422


async def test_alerts_listing(web, state):
    await state.alerts.raise_("node_offline", node_id="rpi3-01", message="gone")
    assert await state.alerts.raise_("node_offline", node_id="rpi3-01", message="again") is None
    r = await web.get("/api/alerts", params={"open": "true"})
    (alert,) = r.json()
    assert alert["kind"] == "node_offline" and alert["level"] == "warning"
    await state.alerts.resolve("node_offline", node_id="rpi3-01")
    assert (await web.get("/api/alerts", params={"open": "true"})).json() == []
    assert len((await web.get("/api/alerts")).json()) == 1


# -- internal listener ---------------------------------------------------------------------------


async def test_internal_requires_service_token(internal, state):
    assert (await internal.get("/internal/api/nodes")).status_code == 401
    assert (
        await internal.get("/internal/api/nodes", headers={"Authorization": "Bearer nope"})
    ).status_code == 401

    row, token = await state.service_tokens.create("telegram-bot", ["read"], actor=ACTOR)
    assert is_token(token, SERVICE_PREFIX)
    auth = {"Authorization": f"Bearer {token}"}
    r = await internal.get("/internal/api/whoami", headers=auth)
    assert r.status_code == 200 and r.json() == {"principal": "telegram-bot", "scopes": ["read"]}
    assert (await internal.get("/internal/api/cluster/summary", headers=auth)).status_code == 200
    assert (await internal.get("/internal/api/alerts", headers=auth)).status_code == 200

    _, approve_only = await state.service_tokens.create("ai-operator", ["approve"], actor=ACTOR)
    r = await internal.get(
        "/internal/api/nodes", headers={"Authorization": f"Bearer {approve_only}"}
    )
    assert r.status_code == 403

    assert await state.service_tokens.revoke(row["id"], actor=ACTOR)
    assert (await internal.get("/internal/api/whoami", headers=auth)).status_code == 401
    listing = await state.service_tokens.list()
    assert listing[0]["revoked_ms"] is not None and "token_hash" not in listing[0]


async def test_internal_node_view_is_trimmed(internal, state, web):
    await web.post("/api/nodes", json=NODE)
    _, token = await state.service_tokens.create("telegram-bot", ["read"], actor=ACTOR)
    r = await internal.get("/internal/api/nodes", headers={"Authorization": f"Bearer {token}"})
    (node,) = r.json()
    assert node["id"] == "rpi3-01" and "static_info" not in node and "has_token" in node


# -- admin listener ------------------------------------------------------------------------------


async def test_admin_socket_operations(admin, state):
    r = await admin.post(
        "/internal/admin/nodes",
        json={"id": "rdkx3-01", "board": "rdkx3"},
        headers={"X-Cli-User": "yuchan"},
    )
    assert r.status_code == 201 and is_token(r.json()["token"], NODE_PREFIX)
    assert (await admin.get("/internal/admin/nodes")).json()[0]["id"] == "rdkx3-01"
    r = await admin.post(
        "/internal/admin/service-tokens",
        json={"principal": "ai-operator", "scopes": ["read", "ai"]},
    )
    assert r.status_code == 201 and is_token(r.json()["token"], SERVICE_PREFIX)
    r = await admin.post(
        "/internal/admin/service-tokens", json={"principal": "mallory", "scopes": ["read"]}
    )
    assert r.status_code == 422
    r = await admin.post("/internal/admin/lockdown", json={"active": True, "reason": "cli"})
    assert r.json()["active"] is True
    status = (await admin.get("/internal/admin/status")).json()
    assert status["lockdown"]["active"] and status["nodes_total"] == 1
    tail = (await admin.get("/internal/admin/audit/tail")).json()
    assert tail[0]["action"] == "system.lockdown"
    assert tail[-1]["actor_type"] == "cli" and tail[-1]["actor_id"] == "yuchan"
    assert (await admin.get("/internal/admin/audit/verify")).json()["ok"]
    assert (await admin.delete("/internal/admin/service-tokens/999")).status_code == 404
    assert (await admin.delete("/internal/admin/nodes/rdkx3-01")).status_code == 204
    assert (await admin.delete("/internal/admin/nodes/rdkx3-01")).status_code == 404


async def test_history_returns_latest_rows_and_merges_buckets(web, state):
    await web.post("/api/nodes", json=NODE)
    base = (time.time() // 60) * 60 - 3600  # an hour ago, so the default until=now covers it
    for i in range(3):
        state.metrics.add("rpi3-01", Metrics.model_validate(metrics(cpu=float(10 * (i + 1)))))
        assert await state.metrics.rollup(now=base + 60 * i) == 1
    # a second rollup into the same bucket merges instead of overwriting
    state.metrics.add("rpi3-01", Metrics.model_validate(metrics(cpu=50.0)))
    assert await state.metrics.rollup(now=base + 120) == 1
    rows = await state.metrics.history("rpi3-01", 0, until_ms=int((base + 1000) * 1000))
    assert [r["cpu_avg"] for r in rows] == [10.0, 20.0, 40.0]  # (30+50)/2 merged
    assert rows[-1]["extra"] == {"samples": 2}
    r = await web.get(
        "/api/nodes/rpi3-01/metrics", params={"history": "true", "since": 0, "limit": 2}
    )
    assert [row["cpu_avg"] for row in r.json()["rows"]] == [20.0, 40.0]  # newest two, ascending
    assert (await web.delete("/api/nodes/rpi3-01")).status_code == 204
    assert await state.metrics.history("rpi3-01", 0, until_ms=int((base + 1000) * 1000)) == []


async def test_patch_and_body_strictness(web):
    await web.post("/api/nodes", json=NODE)
    r = await web.patch("/api/nodes/rpi3-01", json={"sched_reason": "why"})
    assert r.status_code == 422
    r = await web.patch("/api/nodes/rpi3-01", json={})
    assert r.status_code == 422
    r = await web.post(
        "/api/nodes", json={"id": "rpi3-02", "board": "rpi3", "capacity": {"slots": True}}
    )
    assert r.status_code == 422
    r = await web.post(
        "/api/nodes", json={"id": "rpi3-02", "board": "rpi3", "capacity": {"slots": "2"}}
    )
    assert r.status_code == 422
    r = await web.post("/api/nodes", json={"id": "rpi3-02\n", "board": "rpi3"})
    assert r.status_code == 422
    r = await web.get("/api/alerts", params={"before": 10**30})
    assert r.status_code == 422
    r = await web.post(
        "/api/nodes", json={"id": "rpi3-02", "board": "rpi3", "capacity": {"slots": 2}}
    )
    assert r.status_code == 201
