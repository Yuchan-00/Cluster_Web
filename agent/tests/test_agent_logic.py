"""Agent behaviour without a network: output framing and budgets, run bookkeeping, hostile input.
Regression tests for the Phase 1 review findings."""

import asyncio
import json

import pytest

from cluster_agent.agent import OUTPUT_PIECE_CHARS, Agent, encode
from cluster_agent.collectors import MetricsCollector
from cluster_agent.config import CommandLimits
from cluster_agent.executor import (
    OUTPUT_QUEUE_CHUNKS,
    DirectLauncher,
    ExecRequest,
    Executor,
)


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def messages(self, type_=None):
        msgs = [json.loads(d) for d in self.sent]
        return [m for m in msgs if type_ is None or m.get("type") == type_]


def make_agent(limits=None, launcher=None, clock=None):
    agent = Agent(
        "rpi3-01",
        "wss://unused/ws/agent",
        "cat_" + "A" * 43,
        MetricsCollector(board="rpi3", mock_name="rpi3-01"),
        Executor(launcher or DirectLauncher(kill_grace=1.0), flush_interval=0.05),
        command_limits=limits,
        **({"clock": clock} if clock else {}),
    )
    agent._ws = FakeWS()
    agent.connected.set()
    return agent


async def wait_for_result(agent, run_id, timeout=10):
    for _ in range(int(timeout / 0.02)):
        for msg in agent._ws.messages("cmd_result"):
            if msg["run_id"] == run_id:
                return msg
        await asyncio.sleep(0.02)
    raise AssertionError(f"no result for {run_id}")


async def test_output_is_split_and_charged_by_wire_size():
    agent = make_agent(clock=lambda: 100.0)  # frozen: no refill during the test
    await agent._send_output("r1", "stdout", "\x01" * (3 * OUTPUT_PIECE_CHARS + 10))
    frames = agent._ws.sent
    assert len(frames) == 4
    assert all(len(f.encode()) < 64 * 1024 for f in frames)
    charged = 2 * 256 * 1024 - agent._out_bytes.tokens
    assert charged == sum(len(f.encode()) for f in frames)  # escapes are paid for


async def test_output_budget_drops_and_counts():
    agent = make_agent(clock=lambda: 100.0)
    agent._dropped["r1"] = 0
    big = "x" * (OUTPUT_PIECE_CHARS * 200)  # ~1 MB, beyond the 512 KB burst
    await agent._send_output("r1", "stdout", big)
    assert agent._dropped["r1"] > 0
    sent = sum(len(json.loads(f)["data"]) for f in agent._ws.sent)
    assert sent + agent._dropped["r1"] == len(big)


def test_encode_survives_lone_surrogates():
    data = encode({"type": "cmd_result", "run_id": "r", "reason": "bad \ud800"})
    data.encode("utf-8")  # does not raise
    assert json.loads(data)["reason"] == "bad \ud800"


async def test_invalid_run_ids_are_ignored_not_echoed():
    agent = make_agent()
    for run_id in ["\ud800", "a b", "x" * 65, "", 5, None]:
        agent._start_run({"type": "exec", "run_id": run_id, "mode": "shell", "command": "true"})
    await asyncio.sleep(0.1)
    assert agent.pending_results == collections_empty() and agent._ws.sent == []


def collections_empty():
    import collections

    return collections.deque()


async def test_huge_timeout_is_rejected_not_fatal():
    agent = make_agent()
    agent._start_run(
        {"type": "exec", "run_id": "r2", "mode": "shell", "command": "true", "timeout": 10**400}
    )
    result = await wait_for_result(agent, "r2")
    assert result["status"] == "rejected" and "timeout" in result["reason"]


async def test_timeout_limits_from_config():
    agent = make_agent(CommandLimits(default_timeout=7, max_timeout=100))
    agent._start_run(
        {"type": "exec", "run_id": "r3", "mode": "shell", "command": "true", "timeout": 500}
    )
    result = await wait_for_result(agent, "r3")
    assert result["status"] == "rejected" and "100" in result["reason"]
    assert (
        ExecRequest.from_message({"run_id": "x", "mode": "shell", "command": "y"}, 7).timeout == 7
    )


async def test_duplicate_exec_for_running_run_is_ignored():
    agent = make_agent()
    msg = {"type": "exec", "run_id": "r4", "mode": "shell", "command": "sleep 1"}
    agent._start_run(msg)
    await asyncio.sleep(0.2)
    agent._start_run(msg)  # retransmit after reconnect
    result = await wait_for_result(agent, "r4")
    assert result["status"] == "ok"
    await asyncio.sleep(0.2)
    assert len([m for m in agent._ws.messages("cmd_result") if m["run_id"] == "r4"]) == 1


class SlowStart(DirectLauncher):
    async def start(self, req):
        await asyncio.sleep(0.5)  # execd connect + systemd-run start
        return await super().start(req)


async def test_lockdown_cancels_runs_that_are_still_starting():
    agent = make_agent(launcher=SlowStart(kill_grace=1.0))
    agent._start_run({"type": "exec", "run_id": "r5", "mode": "shell", "command": "sleep 5"})
    await asyncio.sleep(0.1)
    assert "r5" in agent.hello()["running_commands"]
    agent._enter_lockdown()
    result = await wait_for_result(agent, "r5")
    assert result["status"] == "cancelled"


async def test_lockdown_before_task_starts():
    agent = make_agent()
    agent._start_run({"type": "exec", "run_id": "r6", "mode": "shell", "command": "echo ran"})
    agent._enter_lockdown()  # same event-loop tick: the run task has not started yet
    result = await wait_for_result(agent, "r6")
    assert result["status"] in ("rejected", "cancelled")


async def test_cancel_while_starting():
    ex = Executor(SlowStart(kill_grace=1.0), flush_interval=0.05)

    async def sink(stream, text):
        pass

    task = asyncio.ensure_future(ex.run(ExecRequest("r7", command="sleep 5"), sink))
    await asyncio.sleep(0.1)
    assert ex.running() == ["r7"]
    assert await ex.cancel("r7") is True
    result = await asyncio.wait_for(task, 10)
    assert result.status == "cancelled"


async def test_output_queue_is_bounded_when_the_sink_stalls():
    ex = Executor(DirectLauncher(kill_grace=1.0), flush_interval=0.05)
    stall = asyncio.Event()

    async def sink(stream, text):
        await stall.wait()  # master link stalled

    task = asyncio.ensure_future(ex.run(ExecRequest("r8", command="yes"), sink))
    await asyncio.sleep(1.0)
    handle = ex._handles["r8"]
    assert handle.queue.qsize() <= OUTPUT_QUEUE_CHUNKS
    stall.set()
    await ex.cancel("r8")
    result = await asyncio.wait_for(task, 10)
    assert result.status == "cancelled"


async def test_bad_messages_do_not_break_dispatch():
    agent = make_agent()
    await agent._dispatch({"type": "config", "metrics_interval": float("nan")})
    await agent._dispatch({"type": "cancel", "run_id": ["x"]})
    await agent._dispatch({"type": "exec", "run_id": "r9", "mode": "preset", "argv": "id"})
    result = await wait_for_result(agent, "r9")
    assert result["status"] == "rejected"
    assert agent.metrics_interval == 5.0


def test_make_ssl_context_requires_a_ca_file():
    from cluster_agent.config import ConfigError
    from cluster_agent.connection import make_ssl_context

    with pytest.raises(ConfigError):
        make_ssl_context(None)
    with pytest.raises(ConfigError):
        make_ssl_context("/nonexistent/ca.pem")
