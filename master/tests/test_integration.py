"""Phase 2 completion check (docs/PLAN.md 18): five real `cluster-agent --mock` nodes, in-process,
against the master's agent listener; a command round-trip; disconnect -> offline."""

from __future__ import annotations

import asyncio
import logging

from cluster_agent.__main__ import _static_extra
from cluster_agent.agent import Agent
from cluster_agent.collectors import MetricsCollector
from cluster_agent.executor import DirectLauncher, Executor

from cluster_master.models import ExecSpec
from cluster_master.ws.agent_hub import new_run_id

from .conftest import register, wait_for

NODES = [
    ("rdkx3-01", "rdkx3"),
    ("rdkx3-02", "rdkx3"),
    ("rpi3-01", "rpi3"),
    ("rpi3-02", "rpi3"),
    ("rpi3-03", "rpi3"),
    ("odroidn2-01", "odroidn2"),
]


async def _set_eq(coro, expected: set[str]) -> bool:
    return await coro == expected


def _mock_agent(url: str, name: str, board: str, token: str) -> Agent:
    collector = MetricsCollector(board=board, mock_name=name)
    extra = _static_extra(collector, {}, {})
    extra["isolation"] = "mock"
    return Agent(
        name,
        url,
        token,
        collector,
        Executor(DirectLauncher()),
        static_extra=extra,
        ssl_ctx=None,
        metrics_interval=0.5,
    )


async def test_mock_cluster(agent_server, state, web, caplog):
    caplog.set_level(logging.INFO)
    agents: list[Agent] = []
    tasks: list[asyncio.Task] = []
    for name, board in NODES:
        token = await register(state, name, board)
        agent = _mock_agent(agent_server.url, name, board, token)
        agents.append(agent)
        tasks.append(asyncio.create_task(agent.run_forever(), name=f"agent-{name}"))
    try:
        await wait_for(lambda: len(state.nodes.online_ids()) == len(NODES), timeout=15)
        await wait_for(
            lambda: all(state.metrics.latest_sample(n) is not None for n, _ in NODES), timeout=15
        )
        nodes = (await web.get("/api/nodes")).json()
        by_id = {n["id"]: n for n in nodes}
        assert all(n["online"] for n in nodes)
        assert by_id["rdkx3-01"]["static_info"]["bpu_cores"] == 2
        assert by_id["rdkx3-01"]["latest"]["bpu"] is not None  # mock rdkx3 reports BPU load
        assert by_id["rpi3-01"]["latest"]["bpu"] is None
        assert by_id["rpi3-01"]["latest"]["temp"] is not None
        # the agent reports the labels it computed; the master keeps its own (empty) set
        assert by_id["rpi3-01"]["labels"] == {}
        assert any("labels" in w for w in by_id["rpi3-01"]["warnings"])
        assert by_id["rpi3-01"]["static_info"]["isolation"] == "mock"
        n2 = by_id["odroidn2-01"]
        assert n2["static_info"]["variant"] == "n2plus" and n2["static_info"]["cpu_count"] == 6
        assert n2["latest"]["bpu"] is None
        extra = state.metrics.latest("odroidn2-01")["data"]["extra"]
        assert {"ddr_temp_c", "cpu_freq_mhz", "thermal_throttle", "freq_capped"} <= set(extra)
        assert set(extra) <= {
            "ddr_temp_c",
            "cpu_freq_mhz",
            "thermal_throttle",
            "freq_capped",
            "emmc_life",
            "reboot_required",
        }  # allow-listed only
        assert extra["cpu_freq_mhz"]["little"] == 1800

        # a real command through the agent's executor
        run_id = new_run_id()
        handle = await state.hub.exec(
            "rpi3-02",
            ExecSpec(run_id=run_id, command="echo hello from $0; echo oops >&2", timeout=10),
        )
        result = await asyncio.wait_for(handle.result, 20)
        assert result.status == "ok" and result.exit_code == 0
        out = []
        while True:
            item = handle.output.get_nowait()
            if item is None:
                break
            out.append(item)
        stdout = "".join(d for s, d in out if s == "stdout")
        stderr = "".join(d for s, d in out if s == "stderr")
        assert stdout.startswith("hello from") and stderr.strip() == "oops"

        # a non-zero exit and a timeout are reported as such
        handle = await state.hub.exec(
            "rpi3-03", ExecSpec(run_id=new_run_id(), command="exit 3", timeout=10)
        )
        result = await asyncio.wait_for(handle.result, 20)
        assert result.status == "error" and result.exit_code == 3
        handle = await state.hub.exec(
            "rpi3-03", ExecSpec(run_id=new_run_id(), command="sleep 30", timeout=1)
        )
        result = await asyncio.wait_for(handle.result, 20)
        assert result.status == "timeout"

        # lockdown: agents refuse exec until unlocked
        await state.lockdown.set(True, actor=("cli", "tester", "cli"), reason="drill")
        await asyncio.sleep(0.3)
        await state.lockdown.set(False, actor=("cli", "tester", "cli"))
        await asyncio.sleep(0.3)
        handle = await state.hub.exec(
            "rpi3-01", ExecSpec(run_id=new_run_id(), command="echo back", timeout=10)
        )
        result = await asyncio.wait_for(handle.result, 20)
        assert result.status == "ok"

        # stopping the agents takes the nodes offline
        await asyncio.gather(*(a.stop() for a in agents[:2]))
        await wait_for(lambda: len(state.nodes.online_ids()) == len(NODES) - 2, timeout=10)

        async def _offline_alerts() -> set[str]:
            alerts = await state.alerts.list(open_only=True)
            return {a["node_id"] for a in alerts if a["kind"] == "node_offline"}

        await wait_for(lambda: _set_eq(_offline_alerts(), {"rdkx3-01", "rdkx3-02"}), timeout=10)
        assert (await web.get("/api/cluster/summary")).json()["nodes_offline"] == 2
        assert (await state.audit.verify()).ok
    finally:
        await asyncio.gather(*(a.stop() for a in agents), return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
