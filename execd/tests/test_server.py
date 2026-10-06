import asyncio
import os
import pwd
import time

import pytest
from conftest import last, output


def shell(command, run_id="r_1", **extra):
    req = {"v": 1, "run_id": run_id, "kind": "command", "mode": "shell", "command": command}
    req.update(extra)
    return req


async def test_runs_and_streams(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call(shell("echo out; echo err >&2; exit 3"))
    assert events[0]["ev"] == "accepted" and events[0]["isolation"] == "fallback"
    assert output(events) == "out\n" and output(events, "stderr") == "err\n"
    exit_ = last(events, "exit")
    assert exit_["status"] == "error" and exit_["exit_code"] == 3
    # command work directories are removed after the run
    assert os.listdir(ctx.paths.work_root) == []


async def test_preset_argv_and_root_op_in_fallback(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call(
        {
            "v": 1,
            "run_id": "r_2",
            "kind": "command",
            "mode": "preset",
            "argv": ["echo", "$HOME", "|", "id"],
        }
    )
    assert output(events) == "$HOME | id\n"  # no shell: nothing is expanded or piped
    events = await client.call(
        {
            "v": 1,
            "run_id": "r_2b",
            "kind": "command",
            "mode": "preset",
            "argv": ["printenv", "HOME"],
        }
    )
    assert output(events) == ctx.paths.workdir("r_2b") + "\n"  # HOME is the work dir


async def test_timeout_kills_process_group(make_server, ctx, tmp_path):
    client = await make_server(ctx)
    pidfile = tmp_path / "bg.pid"
    started = time.monotonic()
    events = await client.call(
        shell(f"sleep 60 & echo $! > {pidfile}; sleep 60", limits={"timeout_s": 1})
    )
    assert last(events, "exit")["status"] == "timeout"
    assert time.monotonic() - started < 15
    await asyncio.sleep(0.2)
    assert not _alive(int(pidfile.read_text()))


async def test_cancel_by_message_and_by_disconnect(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call(shell("sleep 60"), cancel_after=0.5)
    assert last(events, "exit")["status"] == "cancelled"
    # closing the connection cancels too: the run must not outlive the agent's request
    reader, writer = await client.open(shell("sleep 60; touch never", run_id="r_3"))
    await reader.readline()  # accepted
    writer.close()
    await asyncio.sleep(1.5)
    assert not os.path.exists(ctx.paths.workdir("r_3"))


async def test_rejections(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call(shell("id", run_id="../../etc"))
    assert events == [{"ev": "rejected", "reason": "run_id must match ^[A-Za-z0-9_-]{1,64}$"}]
    events = await client.call(shell("id", as_root=True))
    assert events[0]["ev"] == "rejected" and "as_root" in events[0]["reason"]
    stranger = await make_server(ctx, allowed=[os.getuid() + 12345])
    events = await stranger.call(shell("id"))
    assert events == [{"ev": "rejected", "reason": "peer not allowed"}]


async def test_garbage_and_oversized_requests(make_server, ctx):
    client = await make_server(ctx)
    reader, writer = await asyncio.open_unix_connection(client.path)
    writer.write(b"x" * (400 * 1024) + b"\n")
    await writer.drain()
    line = await reader.readline()
    assert b"rejected" in line
    writer.close()


async def test_info_reports_policy_without_argv(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call({"v": 1, "kind": "info"})
    info = events[0]
    assert info["ev"] == "info" and info["isolation"] == "fallback"
    assert info["policy"]["root_ops"]["system.reboot"]["detach"] is True
    assert "argv" not in str(info)


async def test_detached_root_op_needs_systemd(make_server, ctx):
    client = await make_server(ctx)
    events = await client.call(
        {"v": 1, "run_id": "r_4", "kind": "root_op", "root_op": "system.reboot"}
    )
    assert last(events, "exit")["status"] == "failed_to_start"


async def test_memory_watch_marks_oom(make_server, ctx):
    client = await make_server(ctx)
    hog = "python3 -c \"import time; x = b'a' * (120 * 1024 * 1024); time.sleep(20)\""
    events = await client.call(shell(hog, limits={"memory_mb": 80, "timeout_s": 30}))
    exit_ = last(events, "exit")
    assert exit_["status"] == "oom", events


async def test_duplicate_run_id_while_running(make_server, ctx):
    client = await make_server(ctx)
    first = asyncio.ensure_future(client.call(shell("sleep 2", run_id="dup")))
    await asyncio.sleep(0.5)
    events = await client.call(shell("true", run_id="dup"))
    assert last(events, "exit")["reason"] == "duplicate run_id"
    await first


# -- as root: real uid switch -----------------------------------------------------------


@pytest.mark.root
async def test_fallback_switches_to_run_user(make_server, root_ctx):
    secret = os.path.join(os.path.dirname(root_ctx.paths.run_root), "secret")
    with open(secret, "w") as f:
        f.write("token")
    os.chmod(secret, 0o600)
    client = await make_server(root_ctx)
    events = await client.call(shell(f"id -u; id -G; cat {secret}; echo rc=$?"))
    lines = output(events).splitlines()
    uid = pwd.getpwnam(root_ctx.run_user).pw_uid
    assert lines[0] == str(uid)
    assert "0" not in lines[1].split()  # supplementary groups cleared
    assert "rc=1" in lines[-1]  # cannot read a root-only file
    assert "Permission denied" in output(events, "stderr")


@pytest.mark.root
async def test_run_user_cannot_escalate_with_setuid(make_server, root_ctx):
    client = await make_server(root_ctx)
    events = await client.call(shell("cat /proc/self/status | grep NoNewPrivs"))
    assert output(events).split()[-1] == "1"


def _alive(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            state = next(line for line in f if line.startswith("State:"))
    except FileNotFoundError:
        return False
    return "Z" not in state.split()[1]
