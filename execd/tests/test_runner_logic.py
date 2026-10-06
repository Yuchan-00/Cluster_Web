"""Pure logic of the runner: unit result mapping and draining after the agent disappears."""

import asyncio
import os
import time

import pytest
from conftest import last

from cluster_execd.protocol import ClientGone
from cluster_execd.runner import _relay, unit_outcome

LOADED = {"LoadState": "loaded"}


@pytest.mark.parametrize(
    "rc, why, unit, expected",
    [
        (0, "exited", {**LOADED, "Result": "success"}, ("ok", 0)),
        (3, "exited", {**LOADED, "Result": "exit-code", "ExecMainStatus": "3"}, ("error", 3)),
        (1, "exited", {**LOADED, "Result": "oom-kill", "ExecMainStatus": "9"}, ("oom", 1)),
        (1, "exited", {**LOADED, "Result": "timeout", "ExecMainStatus": "15"}, ("timeout", 1)),
        (255, "exited", {**LOADED, "Result": "signal", "ExecMainStatus": "9"}, ("error", None)),
        (1, "exited", {**LOADED, "Result": "resources"}, ("failed_to_start", 1)),
        (0, "exited", {"LoadState": "not-found"}, ("ok", 0)),  # success: already collected
        (1, "exited", {"LoadState": "not-found"}, ("failed_to_start", 1)),  # never created
        (1, "cancelled", {**LOADED, "Result": "signal"}, ("cancelled", 1)),
        (None, "timeout", {}, ("timeout", None)),
    ],
)
def test_unit_outcome(rc, why, unit, expected):
    outcome = unit_outcome(rc, why, unit, "Failed to start transient service unit: bad")
    assert (outcome.status, outcome.exit_code) == expected
    if expected[0] == "failed_to_start":
        assert outcome.reason


class GoneWriter:
    async def send_bytes(self, *args, **kwargs):
        raise ClientGone()


async def test_relay_keeps_draining_after_agent_is_gone():
    reader = asyncio.StreamReader(limit=64 * 1024)
    for _ in range(64):
        reader.feed_data(b"x" * 32 * 1024)  # 2 MiB, far beyond the reader's buffer limit
    reader.feed_eof()
    await asyncio.wait_for(_relay(reader, "stdout", GoneWriter()), 5)
    assert reader.at_eof()


async def test_fallback_run_ends_when_agent_disconnects_mid_output(make_server, ctx):
    client = await make_server(ctx)
    reader, writer = await client.open(
        {"v": 1, "run_id": "flood", "kind": "command", "mode": "shell", "command": "yes"}
    )
    await reader.readline()  # accepted
    await asyncio.sleep(0.3)
    writer.close()  # agent dies while output is flowing
    deadline = time.monotonic() + 15
    while os.path.exists(ctx.paths.workdir("flood")) and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert not os.path.exists(ctx.paths.workdir("flood"))  # run finished and cleaned up


async def test_fallback_flood_completes_for_a_slow_reader(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call(
        {
            "v": 1,
            "run_id": "lots",
            "kind": "command",
            "mode": "shell",
            "command": "head -c 3000000 /dev/zero",
        }
    )
    assert last(events, "exit")["status"] == "ok"
