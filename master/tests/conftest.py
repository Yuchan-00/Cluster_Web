"""Shared fixtures: an AppState on a temp directory, ASGI clients for the three HTTP listeners
and real uvicorn servers (ephemeral ports) for the WebSocket listeners."""

from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
import websockets
from fastapi import FastAPI

from cluster_master.app import Apps, build_apps
from cluster_master.config import MasterConfig
from cluster_master.main import make_server
from cluster_master.state import AppState

ACTOR = ("cli", "tester", "cli")


@pytest.fixture
def config(tmp_path) -> MasterConfig:
    cfg = MasterConfig(data_dir=str(tmp_path / "data"))
    cfg.listeners.internal.path = str(tmp_path / "internal.sock")
    cfg.listeners.admin.path = str(tmp_path / "admin.sock")
    cfg.dev.unauthenticated_admin = True
    # short timings so the offline / hello-timeout paths run in well under a second each
    cfg.agent.metrics_interval_s = 1.0
    cfg.agent.offline_after_s = 2.0
    cfg.agent.hello_timeout_s = 1.0
    cfg.agent.metrics_min_interval_s = 0.05
    cfg.resolve()
    cfg.validate()
    return cfg


@pytest_asyncio.fixture
async def state(config) -> AppState:
    st = AppState.build(config)
    await st.start()
    try:
        yield st
    finally:
        await st.stop()


@pytest.fixture
def apps(state) -> Apps:
    return build_apps(state)


@pytest_asyncio.fixture
async def web(apps):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps.web), base_url="http://web"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def internal(apps):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps.internal, client=None), base_url="http://internal"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def admin(apps):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=apps.admin, client=None), base_url="http://admin"
    ) as client:
        yield client


# -- real servers for WebSocket tests ---------------------------------------------------------


@dataclass
class Served:
    url: str
    port: int
    server: Any
    task: asyncio.Task
    sock: socket.socket


async def _serve(app: FastAPI, ws_max_size: int) -> Served:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    server = make_server(app, ws_max_size=ws_max_size, proxy_headers=True)
    await server.startup(sockets=[sock])
    task = asyncio.create_task(server.main_loop())
    return Served(f"ws://127.0.0.1:{port}", port, server, task, sock)


async def _stop(served: Served) -> None:
    served.server.should_exit = True
    await served.task
    await served.server.shutdown(sockets=[served.sock])
    served.sock.close()


@pytest_asyncio.fixture
async def agent_server(apps, config):
    served = await _serve(apps.agent, config.agent.ws_max_bytes)
    served.url += "/ws/agent"
    try:
        yield served
    finally:
        await _stop(served)


@pytest_asyncio.fixture
async def web_server(apps):
    served = await _serve(apps.web, 64 * 1024)
    try:
        yield served
    finally:
        await _stop(served)


# -- helpers --------------------------------------------------------------------------------


async def register(state: AppState, node_id: str, board: str = "rpi3", **kw) -> str:
    _, token = await state.nodes.register(node_id, board, actor=ACTOR, **kw)
    return token


def hello(node_id: str, board: str = "rpi3", **extra: Any) -> dict[str, Any]:
    msg = {
        "type": "hello",
        "node_id": node_id,
        "board": board,
        "agent_version": "test",
        "static_info": {"hostname": node_id, "cpu_count": 4},
        "running_tasks": [],
        "unacked_results": [],
        "orphaned": [],
        "running_commands": [],
        "pending_results": [],
    }
    msg.update(extra)
    return msg


def metrics(cpu: float = 10.0, **data: Any) -> dict[str, Any]:
    payload = {
        "cpu": {"percent": cpu, "per_core": [cpu] * 4, "load": [0.1, 0.1, 0.1]},
        "mem": {"total": 1024**3, "available": 512 * 1024**2, "percent": 50.0},
        "swap": {"total": 0, "used": 0, "percent": 0.0},
        "disk": [{"mount": "/", "fstype": "ext4", "total": 1, "used": 0, "percent": 30.0}],
        "disk_io": {"read_bps": 0, "write_bps": 0},
        "net": {"eth0": {"rx_bps": 100, "tx_bps": 50}},
        "temp_c": 45.5,
        "uptime_s": 100,
        "procs": 99,
        "extra": {"throttled": "0x0", "junk": "dropped"},
    }
    payload.update(data)
    return {
        "type": "metrics",
        "ts": time.time(),
        "data": payload,
        "sched": {
            "free_slots": 2,
            "free_bpu_slots": 0,
            "job_mem_free_mb": 384,
            "running": [],
            "cached_bundles": [],
        },
    }


class FakeAgent:
    """A hand-driven agent: connect, say hello, then send/receive raw messages."""

    def __init__(self, url: str, node_id: str, token: str) -> None:
        self.url = url
        self.node_id = node_id
        self.token = token
        self.ws: Any = None
        self.welcome: dict[str, Any] | None = None

    async def connect(self, *, hello_msg: dict[str, Any] | None = None, send_hello: bool = True):
        self.ws = await websockets.connect(
            self.url,
            additional_headers={
                "Authorization": f"Bearer {self.token}",
                "X-Node-Id": self.node_id,
            },
            max_size=1024 * 1024,
            open_timeout=5,
        )
        if send_hello:
            await self.send(hello_msg or hello(self.node_id))
            self.welcome = await self.recv()
        return self

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg, separators=(",", ":")))

    async def send_raw(self, raw: str | bytes) -> None:
        await self.ws.send(raw)

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        raw = await asyncio.wait_for(self.ws.recv(), timeout)
        return json.loads(raw)

    async def closed_with(self, timeout: float = 5.0) -> int:
        """Wait for the server to close the socket; return the close code."""
        try:
            while True:
                await asyncio.wait_for(self.ws.recv(), timeout)
        except websockets.ConnectionClosed as exc:
            return exc.rcvd.code if exc.rcvd else -1

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()


async def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


__all__ = [
    "ACTOR",
    "FakeAgent",
    "Served",
    "hello",
    "metrics",
    "register",
    "wait_for",
]
