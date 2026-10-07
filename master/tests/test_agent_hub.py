"""Agent hub over a real uvicorn listener with `websockets` clients (security.md 8)."""

from __future__ import annotations

import asyncio
import json
import time

import pytest
import websockets

from cluster_master.models import ExecSpec
from cluster_master.ws.agent_hub import (
    CLOSE_AUTH,
    CLOSE_DUPLICATE,
    CLOSE_IDENTITY,
    CLOSE_RATE,
    CLOSE_SILENT,
    MAX_VIOLATIONS,
    LockdownActive,
    NodeOffline,
    new_run_id,
)

from .conftest import ACTOR, FakeAgent, hello, metrics, register, wait_for


async def _status(agent_server, headers) -> int:
    try:
        ws = await websockets.connect(agent_server.url, additional_headers=headers, open_timeout=5)
    except websockets.InvalidStatus as exc:
        return exc.response.status_code
    await ws.close()
    return 101


async def test_handshake_auth(agent_server, state):
    token = await register(state, "rpi3-01")
    good = {"Authorization": f"Bearer {token}", "X-Node-Id": "rpi3-01"}
    assert await _status(agent_server, {}) == 401
    assert await _status(agent_server, {"X-Node-Id": "rpi3-01"}) == 401
    assert await _status(agent_server, dict(good, Authorization="Bearer cat_" + "x" * 43)) == 401
    assert await _status(agent_server, {**good, "X-Node-Id": "rpi3-02"}) == 401  # unknown node
    assert await _status(agent_server, {**good, "X-Node-Id": "RPI3-01"}) == 401
    assert await _status(agent_server, dict(good, Authorization=f"Basic {token}")) == 401
    assert await _status(agent_server, good) == 101
    await state.nodes.revoke_token("rpi3-01", actor=ACTOR)
    assert await _status(agent_server, good) == 401
    # one audit row per peer per window, not one per attempt
    rows = await state.audit.tail()
    assert [r["action"] for r in rows].count("agent.auth_failed") == 1


async def test_auth_failure_flood_raises_alert(agent_server, state):
    await register(state, "rpi3-01")
    bad = {"Authorization": "Bearer cat_" + "y" * 43, "X-Node-Id": "rpi3-01"}
    for _ in range(5):
        assert await _status(agent_server, bad) == 401
    alerts = await state.alerts.list(open_only=True)
    assert [a["kind"] for a in alerts] == ["agent_auth_failures"]
    assert alerts[0]["node_id"] == "rpi3-01" and alerts[0]["level"] == "critical"


async def test_hello_timeout_and_identity_pinning(agent_server, state):
    token = await register(state, "rpi3-01")
    await register(state, "rpi3-02")
    silent = await FakeAgent(agent_server.url, "rpi3-01", token).connect(send_hello=False)
    t0 = time.monotonic()
    assert await silent.closed_with() == 1008
    assert time.monotonic() - t0 < 3

    liar = await FakeAgent(agent_server.url, "rpi3-01", token).connect(send_hello=False)
    await liar.send(hello("rpi3-02"))
    assert await liar.closed_with() == CLOSE_IDENTITY
    alerts = await state.alerts.list(open_only=True)
    assert [a["kind"] for a in alerts] == ["agent_identity"]
    assert any(r["action"] == "agent.identity_mismatch" for r in await state.audit.tail())
    assert not state.nodes.status("rpi3-01").online

    wrong_type = await FakeAgent(agent_server.url, "rpi3-01", token).connect(send_hello=False)
    await wrong_type.send(metrics())
    assert await wrong_type.closed_with() == 1008

    binary = await FakeAgent(agent_server.url, "rpi3-01", token).connect(send_hello=False)
    await binary.send_raw(b"\x00")
    assert await binary.closed_with() == 1008


async def test_five_agents_online_then_offline(agent_server, state, web):
    agents = []
    for i in range(1, 6):
        name = f"rpi3-0{i}" if i <= 3 else f"rdkx3-0{i - 3}"
        board = "rpi3" if i <= 3 else "rdkx3"
        token = await register(state, name, board)
        agent = await FakeAgent(agent_server.url, name, token).connect(hello_msg=hello(name, board))
        assert agent.welcome["type"] == "welcome"
        assert agent.welcome["lockdown"] is False
        assert agent.welcome["metrics_interval"] == 1.0
        await agent.send(metrics(cpu=float(i)))
        agents.append(agent)

    await wait_for(lambda: all(state.metrics.latest_sample(a.node_id) for a in agents))
    nodes = (await web.get("/api/nodes")).json()
    assert len(nodes) == 5 and all(n["online"] for n in nodes)
    by_id = {n["id"]: n for n in nodes}
    assert by_id["rpi3-02"]["latest"]["cpu"] == 2.0
    assert by_id["rdkx3-01"]["sched"]["free_slots"] == 2
    assert by_id["rpi3-01"]["static_info"]["hostname"] == "rpi3-01"
    summary = (await web.get("/api/cluster/summary")).json()
    assert summary["nodes_online"] == 5 and summary["nodes_offline"] == 0
    assert len((await web.get("/api/system/status")).json()["agents"]) == 5
    assert await state.alerts.list(open_only=True) == []

    # three close cleanly
    for agent in agents[:3]:
        await agent.close()
    await wait_for(lambda: len(state.nodes.online_ids()) == 2)
    # two go silent: the watchdog closes them after offline_after_s (2 s here)
    t0 = time.monotonic()
    codes = await asyncio.gather(*(a.closed_with(timeout=6) for a in agents[3:]))
    assert codes == [CLOSE_SILENT, CLOSE_SILENT]
    assert 1.0 < time.monotonic() - t0 < 5.0
    await wait_for(lambda: len(state.nodes.online_ids()) == 0)
    nodes = (await web.get("/api/nodes")).json()
    assert all(not n["online"] for n in nodes)
    assert all(n["last_seen_ms"] is not None for n in nodes)
    reasons = {n["id"]: n["disconnect_reason"] for n in nodes}
    assert reasons["rdkx3-02"] == "no messages"
    assert reasons["rpi3-01"].startswith("agent closed")

    async def _open_alerts():
        return await state.alerts.list(open_only=True)

    async def _all_offline_alerts() -> bool:
        return sorted(a["node_id"] for a in await _open_alerts()) == sorted(n["id"] for n in nodes)

    await wait_for(_all_offline_alerts)  # alerts are written just after the status flips
    assert {a["kind"] for a in await _open_alerts()} == {"node_offline"}

    # coming back resolves the offline alert
    agent = await FakeAgent(
        agent_server.url, "rpi3-01", await state.nodes.rotate_token("rpi3-01", actor=ACTOR)
    ).connect()
    await wait_for(lambda: state.nodes.status("rpi3-01").online)
    open_alerts = await state.alerts.list(open_only=True)
    assert "rpi3-01" not in {a["node_id"] for a in open_alerts}
    await agent.close()


async def test_duplicate_connection_is_refused(agent_server, state):
    token = await register(state, "rpi3-01")
    first = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    await first.send(metrics())
    second = FakeAgent(agent_server.url, "rpi3-01", token)
    await second.connect(send_hello=False)
    await second.send(hello("rpi3-01"))
    assert await second.closed_with() == CLOSE_DUPLICATE
    alerts = await state.alerts.list(open_only=True)
    assert [a["kind"] for a in alerts] == ["agent_duplicate"]
    assert any(r["action"] == "agent.duplicate" for r in await state.audit.tail())
    # the first connection is untouched
    await asyncio.sleep(0.1)  # past metrics_min_interval_s
    await first.send(metrics(cpu=77.0))
    await wait_for(lambda: state.metrics.latest_sample("rpi3-01").cpu == 77.0)
    assert state.nodes.status("rpi3-01").online
    await first.close()


async def test_stale_connection_is_replaced_without_alert(agent_server, state, config):
    token = await register(state, "rpi3-01")
    first = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    # the hub treats a connection silent for > 2 * metrics_interval as dead
    conn = state.hub._conns["rpi3-01"]
    conn.last_message -= 2 * config.agent.metrics_interval_s + 1
    second = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    assert second.welcome["type"] == "welcome"
    assert await first.closed_with() == CLOSE_DUPLICATE
    assert await state.alerts.list(open_only=True) == []
    assert state.hub._conns["rpi3-01"].peer is not None
    await second.send(metrics(cpu=5.0))
    await wait_for(lambda: state.metrics.latest_sample("rpi3-01") is not None)
    await second.close()


async def test_protocol_violations_close_after_limit(agent_server, state):
    token = await register(state, "rpi3-01")
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    for _ in range(MAX_VIOLATIONS - 1):
        await agent.send({"type": "cmd_result", "run_id": "never-issued", "status": "ok"})
        await asyncio.sleep(0.01)
    await agent.send(metrics(cpu=1.0))  # still alive
    await wait_for(lambda: state.metrics.latest_sample("rpi3-01") is not None)
    await agent.send({"type": "bogus"})
    assert await agent.closed_with() == 1008
    await wait_for(lambda: not state.nodes.status("rpi3-01").online)


async def test_metrics_extra_is_filtered_and_limited(agent_server, state, config):
    token = await register(state, "rpi3-01")
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    await agent.send(metrics())
    await wait_for(lambda: state.metrics.latest("rpi3-01") is not None)
    assert state.metrics.latest("rpi3-01")["data"]["extra"] == {"throttled": "0x0"}

    # an oversized allow-listed value is dropped and counted, the sample is still used
    await asyncio.sleep(config.agent.metrics_min_interval_s * 2)
    big = metrics(cpu=9.0)
    big["data"]["extra"] = {"bpu": [1] * 3000}
    await agent.send(big)
    await wait_for(lambda: state.metrics.latest_sample("rpi3-01").cpu == 9.0)
    assert state.metrics.latest("rpi3-01")["data"]["extra"] == {}
    assert state.hub._conns["rpi3-01"].violations == 1

    # NaN never reaches the store
    await asyncio.sleep(config.agent.metrics_min_interval_s * 2)
    await agent.send_raw(json.dumps(metrics()).replace('"percent": 50.0', '"percent": NaN'))
    await asyncio.sleep(0.1)
    assert state.hub._conns["rpi3-01"].violations == 2
    await agent.close()


async def test_metrics_too_fast_is_a_violation(agent_server, state, config):
    config.agent.metrics_min_interval_s = 2.0  # the hub reads the shared object
    token = await register(state, "rpi3-01")
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    await agent.send(metrics(cpu=1.0))
    await agent.send(metrics(cpu=2.0))
    await wait_for(lambda: state.hub._conns["rpi3-01"].violations == 1)
    assert state.metrics.latest_sample("rpi3-01").cpu == 1.0
    await agent.close()


async def test_rate_limit_closes_with_4429(agent_server, state):
    token = await register(state, "rpi3-01")
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    for _ in range(200):
        await agent.send({"type": "pong"})
    assert await agent.closed_with() == CLOSE_RATE
    alerts = await state.alerts.list(open_only=True)
    assert {a["kind"] for a in alerts} >= {"agent_rate_limit"}


async def test_static_info_size_limit(agent_server, state, config):
    token = await register(state, "rpi3-01")
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect(send_hello=False)
    big = hello("rpi3-01", static_info={"blob": "x" * (config.agent.static_info_max_bytes + 10)})
    await agent.send(big)
    assert await agent.closed_with() == 1009


async def test_board_mismatch_and_reported_labels_are_warnings(agent_server, state):
    token = await register(state, "rpi3-01", labels={"zone": "a"})
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect(
        hello_msg=hello(
            "rpi3-01",
            board="rdkx3",
            static_info={"labels": {"bpu": True}, "capacity": {"slots": 9}},
        )
    )
    status = state.nodes.status("rpi3-01")
    assert status.online
    assert any("board" in w for w in status.warnings)
    assert any("labels" in w for w in status.warnings)
    assert any("capacity" in w for w in status.warnings)
    assert state.nodes.get("rpi3-01").labels == {"zone": "a"}  # registry unchanged
    alerts = await state.alerts.list(open_only=True)
    assert [a["kind"] for a in alerts] == ["node_board_mismatch"]
    await agent.close()


async def test_exec_roundtrip_and_ownership(agent_server, state):
    token_a = await register(state, "rpi3-01")
    token_b = await register(state, "rpi3-02")
    a = await FakeAgent(agent_server.url, "rpi3-01", token_a).connect()
    b = await FakeAgent(agent_server.url, "rpi3-02", token_b).connect()
    finished = []
    state.bus.subscribe(lambda e: finished.append(e.data), "command.finished")

    with pytest.raises(NodeOffline):
        await state.hub.exec("rpi3-09", ExecSpec(run_id=new_run_id(), command="true"))

    run_id = new_run_id()
    handle = await state.hub.exec("rpi3-01", ExecSpec(run_id=run_id, command="echo hi", timeout=5))
    msg = await a.recv()
    assert msg["type"] == "exec" and msg["run_id"] == run_id and msg["command"] == "echo hi"
    assert state.nodes.status("rpi3-01").running_commands == [run_id]

    # B tries to speak for A's run: counted as a violation, never delivered
    await b.send({"type": "cmd_output", "run_id": run_id, "stream": "stdout", "data": "evil"})
    await b.send({"type": "cmd_result", "run_id": run_id, "status": "ok", "exit_code": 0})
    await wait_for(lambda: state.hub._conns["rpi3-02"].violations == 2)
    assert handle.output.empty() and not handle.result.done()

    await a.send({"type": "cmd_output", "run_id": run_id, "stream": "stdout", "data": "hi\n"})
    await a.send({"type": "cmd_output", "run_id": run_id, "stream": "stderr", "data": "warn"})
    await a.send(
        {"type": "cmd_result", "run_id": run_id, "status": "ok", "exit_code": 0, "duration_ms": 3}
    )
    result = await asyncio.wait_for(handle.result, 5)
    assert result.status == "ok" and result.exit_code == 0
    chunks = []
    while True:
        item = handle.output.get_nowait()
        if item is None:
            break
        chunks.append(item)
    assert chunks == [("stdout", "hi\n"), ("stderr", "warn")]
    assert handle.output_bytes == 7
    assert finished == [{"run_id": run_id, "node_id": "rpi3-01", "status": "ok"}]
    assert state.nodes.status("rpi3-01").running_commands == []
    assert state.hub.run(run_id) is None

    # duplicate run ids are refused; cancel reaches the agent
    run2 = new_run_id()
    await state.hub.exec("rpi3-01", ExecSpec(run_id=run2, command="sleep 10"))
    with pytest.raises(Exception, match="already issued"):
        await state.hub.exec("rpi3-01", ExecSpec(run_id=run2, command="x"))
    await a.recv()  # the exec
    assert await state.hub.cancel("rpi3-01", run2)
    assert (await a.recv()) == {"type": "cancel", "run_id": run2}
    assert not await state.hub.cancel("rpi3-02", run2)  # not B's run
    await a.close()
    await b.close()


async def test_lockdown_broadcast_and_exec_refusal(agent_server, state):
    token_a = await register(state, "rpi3-01")
    token_b = await register(state, "rpi3-02")
    a = await FakeAgent(agent_server.url, "rpi3-01", token_a).connect()
    b = await FakeAgent(agent_server.url, "rpi3-02", token_b).connect()
    run_id = new_run_id()
    handle = await state.hub.exec("rpi3-01", ExecSpec(run_id=run_id, command="sleep 30"))
    await a.recv()

    assert await state.lockdown.set(True, actor=ACTOR, reason="test")
    assert (await a.recv()) == {"type": "lockdown"}
    assert (await b.recv()) == {"type": "lockdown"}
    result = await asyncio.wait_for(handle.result, 5)
    assert result.status == "cancelled" and result.reason == "lockdown"
    with pytest.raises(LockdownActive):
        await state.hub.exec("rpi3-01", ExecSpec(run_id=new_run_id(), command="true"))

    token_c = await register(state, "rpi3-03")
    c = await FakeAgent(agent_server.url, "rpi3-03", token_c).connect()
    assert c.welcome["lockdown"] is True

    await state.lockdown.set(False, actor=ACTOR)
    for agent in (a, b, c):
        assert (await agent.recv()) == {"type": "unlock"}
    for agent in (a, b, c):
        await agent.close()


async def test_token_revocation_disconnects_live_agent(agent_server, state):
    token = await register(state, "rpi3-01")
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    await state.nodes.revoke_token("rpi3-01", actor=ACTOR)
    assert await agent.closed_with() == CLOSE_AUTH
    await wait_for(lambda: not state.nodes.status("rpi3-01").online)

    new_token = await state.nodes.rotate_token("rpi3-01", actor=ACTOR)
    agent = await FakeAgent(agent_server.url, "rpi3-01", new_token).connect()
    await state.nodes.remove("rpi3-01", actor=ACTOR)
    assert await agent.closed_with() == CLOSE_AUTH
    assert "rpi3-01" not in state.hub._conns
    # a removed node leaves no offline alert behind
    assert all(a["node_id"] != "rpi3-01" for a in await state.alerts.list(open_only=True))


async def test_ui_hub_streams_events(agent_server, web_server, state):
    token = await register(state, "rpi3-01")
    ui = await websockets.connect(web_server.url + "/ws/ui", open_timeout=5)
    first = json.loads(await asyncio.wait_for(ui.recv(), 5))
    assert first["type"] == "hello" and first["user"] == "dev"
    agent = await FakeAgent(agent_server.url, "rpi3-01", token).connect()
    await agent.send(metrics(cpu=12.0))
    seen = {}
    for _ in range(3):
        evt = json.loads(await asyncio.wait_for(ui.recv(), 5))
        assert evt["type"] == "event"
        seen[evt["event"]] = evt["data"]
        if "metrics" in seen:
            break
    assert seen["node.online"]["node_id"] == "rpi3-01"
    assert seen["metrics"]["sample"]["cpu"] == 12.0
    await ui.send(json.dumps({"type": "ping"}))
    assert json.loads(await asyncio.wait_for(ui.recv(), 5))["type"] == "pong"
    await ui.send("not json")
    with pytest.raises(websockets.ConnectionClosed) as exc:
        await asyncio.wait_for(ui.recv(), 5)
    assert exc.value.rcvd.code == 1008
    await agent.close()


async def test_ui_hub_checks_origin(web_server):
    with pytest.raises(websockets.InvalidStatus) as exc:
        await websockets.connect(
            web_server.url + "/ws/ui",
            additional_headers={"Origin": "https://evil.example"},
            open_timeout=5,
        )
    assert exc.value.response.status_code == 403
    host = f"127.0.0.1:{web_server.port}"
    ws = await websockets.connect(
        web_server.url + "/ws/ui", additional_headers={"Origin": f"http://{host}"}, open_timeout=5
    )
    assert json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] == "hello"
    await ws.close()
