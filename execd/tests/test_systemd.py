"""Isolation mode A against a real systemd (PID 1). Runs in CI under sudo; skipped elsewhere.

These check the security.md 11.2 properties actually apply on a stock systemd, which the
argv tests in test_units.py cannot prove.
"""

import asyncio
import os
import pwd
import shutil
import subprocess
import tempfile

import pytest
from conftest import last, output

from cluster_execd.runner import Context
from cluster_execd.units import Paths

pytestmark = pytest.mark.systemd


@pytest.fixture
def systemd_ctx(policy, run_user):
    if not os.path.isdir("/run/systemd/system") or not shutil.which("systemd-run"):
        pytest.skip("needs systemd as PID 1")
    # Not under /tmp or /var/tmp: PrivateTmp=yes would hide the work directory.
    base = tempfile.mkdtemp(prefix="cwtest-", dir="/var/lib")
    os.chmod(base, 0o755)
    paths = Paths(
        run_root=os.path.join(base, "run"),
        agent_root=os.path.join(base, "agent"),
        agent_etc=os.path.join(base, "etc-agent"),
        execd_etc=os.path.join(base, "etc"),
        state_dir=os.path.join(base, "state"),
        systemd_run=shutil.which("systemd-run"),
        systemctl=shutil.which("systemctl"),
    )
    for d in (paths.agent_root, paths.agent_etc, paths.execd_etc):
        os.makedirs(d, mode=0o755)
    with open(paths.execd_socket, "w") as f:  # stands in for the socket
        f.write("")
    with open(os.path.join(paths.agent_etc, "agent.token"), "w") as f:
        f.write("secret")
    yield Context(policy=policy, paths=paths, run_user=run_user, isolation="systemd")
    shutil.rmtree(base, ignore_errors=True)


def shell(command, run_id, **extra):
    req = {"v": 1, "run_id": run_id, "kind": "command", "mode": "shell", "command": command}
    req.update(extra)
    return req


async def test_runs_as_run_user_with_exit_code(make_server, systemd_ctx):
    client = await make_server(systemd_ctx)
    events = await client.call(shell("id -u; echo err >&2; exit 3", "sd_1"))
    uid = pwd.getpwnam(systemd_ctx.run_user).pw_uid
    assert output(events).strip() == str(uid)
    assert "err" in output(events, "stderr")
    exit_ = last(events, "exit")
    assert exit_["status"] == "error" and exit_["exit_code"] == 3, events


async def test_sandbox_properties(make_server, systemd_ctx):
    os.makedirs(os.path.join(systemd_ctx.paths.work_root, "other_run"), exist_ok=True)
    client = await make_server(systemd_ctx)
    script = "; ".join(
        [
            f"ls {systemd_ctx.paths.work_root}",
            "grep NoNewPrivs /proc/self/status",
            f"cat {systemd_ctx.paths.agent_etc}/agent.token || echo token:denied",
            f"cat {systemd_ctx.paths.execd_socket} || echo socket:denied",
            "touch /usr/local/x 2>/dev/null || echo rootfs:readonly",
            "touch ./ok && echo workdir:writable",
        ]
    )
    events = await client.call(shell(script, "sd_2"))
    out = output(events)
    assert out.splitlines()[0] == "sd_2"  # other runs' directories are hidden
    assert "NoNewPrivs:\t1" in out
    assert "token:denied" in out and "secret" not in out
    assert "socket:denied" in out
    assert "rootfs:readonly" in out
    assert "workdir:writable" in out
    assert last(events, "exit")["status"] == "ok", events


async def test_private_network(make_server, systemd_ctx):
    client = await make_server(systemd_ctx)
    events = await client.call(
        shell("cat /proc/net/dev | tail -n +3 | cut -d: -f1", "sd_3", network="none")
    )
    assert [x.strip() for x in output(events).split()] == ["lo"], events


async def test_timeout_by_runtime_max(make_server, systemd_ctx):
    client = await make_server(systemd_ctx)
    events = await client.call(shell("sleep 60", "sd_4", limits={"timeout_s": 2}))
    assert last(events, "exit")["status"] == "timeout", events


async def test_cancel_stops_whole_unit(make_server, systemd_ctx):
    client = await make_server(systemd_ctx)
    events = await client.call(shell("setsid sleep 60 & sleep 60", "sd_5"), cancel_after=2)
    assert last(events, "exit")["status"] == "cancelled", events
    await asyncio.sleep(1)
    state = subprocess.run(
        ["systemctl", "is-active", "cluster-run-sd_5.service"], capture_output=True, text=True
    ).stdout.strip()
    assert state in ("inactive", "failed", "unknown", ""), state
    # setsid escaped the process group but not the cgroup
    leftovers = subprocess.run(
        ["pgrep", "-u", systemd_ctx.run_user, "-f", "sleep 60"], capture_output=True, text=True
    ).stdout.strip()
    assert leftovers == ""


async def test_memory_limit_maps_to_oom(make_server, systemd_ctx):
    client = await make_server(systemd_ctx)
    hog = "python3 -c \"x = b'a' * (300 * 1024 * 1024)\""
    events = await client.call(shell(hog, "sd_6", limits={"memory_mb": 64}))
    assert last(events, "exit")["status"] == "oom", events
