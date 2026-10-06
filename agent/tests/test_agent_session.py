"""End-to-end tests of the agent against a fake master over real TLS with the internal CA."""

import asyncio
import json
import os
import ssl
import subprocess

import pytest
import websockets

from cluster_agent import agent as agent_mod
from cluster_agent.agent import AUTH_RETRY_S, Agent
from cluster_agent.collectors import MetricsCollector
from cluster_agent.config import ConfigError
from cluster_agent.connection import load_token, make_ssl_context
from cluster_agent.executor import DirectLauncher, Executor

PKI = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "pki", "cluster-pki.sh")
TOKEN = "cat_" + "A" * 43


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    base = tmp_path_factory.mktemp("pki")
    env = dict(os.environ, CLUSTER_PKI_NO_PASSPHRASE="1")

    def run(*args):
        subprocess.run(["sh", PKI, *args], check=True, env=env, capture_output=True)

    run("init-ca", str(base / "ca"))
    run("issue", str(base / "ca"), "localhost", "DNS:localhost", "IP:127.0.0.1")
    run("issue", str(base / "ca"), "master.cluster.internal")  # right CA, wrong name
    run("init-ca", str(base / "other"))
    run("issue", str(base / "other"), "localhost", "DNS:localhost")  # wrong CA
    return base


def server_ctx(pki, ca="ca", name="localhost"):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(pki / ca / f"{name}.pem"), str(pki / ca / f"{name}.key"))
    return ctx


def headers_of(ws):
    request = getattr(ws, "request", None)  # websockets >= 13 new implementation
    return request.headers if request is not None else ws.request_headers


class FakeMaster:
    def __init__(self, welcome=None, reject=False):
        self.welcome = welcome or {"type": "welcome", "metrics_interval": 1}
        self.reject = reject
        self.inbox = asyncio.Queue()
        self.headers = []
        self.connections = []
        self.connected = asyncio.Event()

    async def handler(self, ws, *_):
        self.headers.append({k.lower(): v for k, v in headers_of(ws).items()})
        self.connections.append(ws)
        hello = json.loads(await ws.recv())
        await self.inbox.put(hello)
        if self.reject:
            await ws.close(4401, "bad token")
            return
        await ws.send(json.dumps(self.welcome))
        self.connected.set()
        try:
            async for raw in ws:
                await self.inbox.put(json.loads(raw))
        except websockets.ConnectionClosed:
            pass

    async def send(self, msg):
        await self.connections[-1].send(json.dumps(msg))

    async def expect(self, type_, timeout=10, **match):
        while True:
            msg = await asyncio.wait_for(self.inbox.get(), timeout)
            if msg.get("type") == type_ and all(msg.get(k) == v for k, v in match.items()):
                return msg


@pytest.fixture
async def cluster(pki):
    started = []

    async def start(
        welcome=None, server_ca="ca", server_name="localhost", client_ca="ca", reject=False
    ):
        master = FakeMaster(welcome, reject)
        server = await websockets.serve(
            master.handler, "127.0.0.1", 0, ssl=server_ctx(pki, server_ca, server_name)
        )
        port = server.sockets[0].getsockname()[1]
        agent = Agent(
            "rpi3-01",
            f"wss://localhost:{port}/ws/agent",
            TOKEN,
            MetricsCollector(board="rpi3", mock_name="rpi3-01"),
            Executor(DirectLauncher(kill_grace=1.0), flush_interval=0.05),
            static_extra={"labels": {"board": "rpi3"}, "capacity": {"slots": 2}},
            ssl_ctx=make_ssl_context(str(pki / client_ca / "ca.pem")),
            metrics_interval=1,
        )
        task = asyncio.ensure_future(agent.run_forever())
        started.append((server, agent, task))
        return master, agent

    yield start
    for server, agent, task in started:
        await agent.stop()
        task.cancel()
        server.close()


async def test_hello_headers_and_metrics(cluster):
    master, agent = await cluster()
    hello = await master.expect("hello")
    assert master.headers[0]["authorization"] == f"Bearer {TOKEN}"
    assert master.headers[0]["x-node-id"] == "rpi3-01"
    assert "token" not in json.dumps(hello).lower()  # token travels only in the header
    assert hello["board"] == "rpi3" and hello["static_info"]["labels"] == {"board": "rpi3"}
    metrics = await master.expect("metrics")
    assert metrics["data"]["extra"].keys() <= set(agent_mod.EXTRA_KEYS)
    assert metrics["sched"]["free_slots"] == 2
    assert "ts" in metrics and "ts" not in metrics["data"]


async def test_exec_streams_output_and_result(cluster):
    master, agent = await cluster()
    await master.connected.wait()
    await master.send(
        {
            "type": "exec",
            "run_id": "r1",
            "mode": "shell",
            "command": "echo hello; echo oops >&2; exit 4",
            "timeout": 10,
        }
    )
    out = await master.expect("cmd_output", run_id="r1", stream="stdout")
    assert out["data"] == "hello\n"
    result = await master.expect("cmd_result", run_id="r1")
    assert result["status"] == "error" and result["exit_code"] == 4


async def test_cancel(cluster):
    master, agent = await cluster()
    await master.connected.wait()
    await master.send({"type": "exec", "run_id": "r2", "mode": "shell", "command": "sleep 30"})
    await asyncio.sleep(0.3)
    await master.send({"type": "cancel", "run_id": "r2"})
    result = await master.expect("cmd_result", run_id="r2")
    assert result["status"] == "cancelled"


async def test_lockdown_from_welcome_then_unlock(cluster):
    master, agent = await cluster(welcome={"type": "welcome", "lockdown": True})
    await master.connected.wait()
    await master.send({"type": "exec", "run_id": "r3", "mode": "shell", "command": "true"})
    result = await master.expect("cmd_result", run_id="r3")
    assert result == {
        "type": "cmd_result",
        "run_id": "r3",
        "status": "rejected",
        "exit_code": None,
        "duration_ms": 0,
        "output_bytes": 0,
        "truncated": False,
        "reason": "lockdown",
    }
    await master.send({"type": "unlock"})
    await master.send({"type": "exec", "run_id": "r4", "mode": "shell", "command": "true"})
    assert (await master.expect("cmd_result", run_id="r4"))["status"] == "ok"


async def test_lockdown_message_cancels_running(cluster):
    master, agent = await cluster()
    await master.connected.wait()
    await master.send({"type": "exec", "run_id": "r5", "mode": "shell", "command": "sleep 30"})
    await asyncio.sleep(0.3)
    await master.send({"type": "lockdown"})
    assert (await master.expect("cmd_result", run_id="r5"))["status"] == "cancelled"


async def test_bad_exec_messages_are_answered(cluster):
    master, agent = await cluster()
    await master.connected.wait()
    await master.send({"type": "exec", "run_id": "r6", "mode": "telnet"})
    result = await master.expect("cmd_result", run_id="r6")
    assert result["status"] == "rejected"
    await master.send({"type": "future_message", "x": 1})  # ignored, connection stays up
    await master.send({"type": "exec", "run_id": "r7", "mode": "preset", "argv": ["true"]})
    assert (await master.expect("cmd_result", run_id="r7"))["status"] == "ok"


async def test_result_survives_reconnect(cluster):
    master, agent = await cluster()
    await master.expect("hello")  # the first session's hello
    await master.connected.wait()
    await master.send(
        {"type": "exec", "run_id": "r8", "mode": "shell", "command": "sleep 2.5; echo done"}
    )
    await asyncio.sleep(0.2)
    await master.connections[-1].close()  # master restarts while the command runs
    hello = await master.expect("hello")
    assert hello["running_commands"] == ["r8"]  # still running across the reconnect
    result = await master.expect("cmd_result", run_id="r8", timeout=15)
    assert result["status"] == "ok"


async def test_auth_rejection_backs_off(cluster, monkeypatch):
    delays = []
    original = Agent._retry_delay

    def spy(self, exc, default):
        delay = original(self, exc, default)
        delays.append(delay)
        return delay

    monkeypatch.setattr(Agent, "_retry_delay", spy)
    master, agent = await cluster(reject=True)  # master closes with 4401 after hello
    await master.expect("hello")
    for _ in range(50):
        if delays:
            break
        await asyncio.sleep(0.05)
    assert delays == [AUTH_RETRY_S]
    assert not agent.connected.is_set()


@pytest.mark.parametrize(
    "server_ca, server_name", [("other", "localhost"), ("ca", "master.cluster.internal")]
)
async def test_tls_refuses_wrong_ca_or_name(cluster, server_ca, server_name):
    master, agent = await cluster(server_ca=server_ca, server_name=server_name)
    await asyncio.sleep(1.0)
    assert master.inbox.empty()  # no hello: the handshake failed before any data
    assert not agent.connected.is_set()


def test_token_file_rules(tmp_path):
    path = tmp_path / "agent.token"
    path.write_text(TOKEN + "\n")
    os.chmod(path, 0o600)
    assert load_token(str(path)) == TOKEN
    os.chmod(path, 0o640)
    with pytest.raises(ConfigError, match="chmod 600"):
        load_token(str(path))
    os.chmod(path, 0o600)
    path.write_text("cat_short")
    with pytest.raises(ConfigError, match="node token"):
        load_token(str(path))
    link = tmp_path / "link.token"
    link.symlink_to(path)
    with pytest.raises(ConfigError, match="cannot open"):
        load_token(str(link))
