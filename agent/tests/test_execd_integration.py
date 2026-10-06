"""cluster-agent's execd client against the real cluster-execd server (in-process, fallback
mode, no uid switch), so both ends of the socket protocol are exercised together."""

import asyncio
import os
import pwd
import sys

import pytest

EXECD = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "execd"))
sys.path.insert(0, EXECD)
execd_policy = pytest.importorskip("cluster_execd.policy")
from cluster_execd.runner import Context, make_workdir  # noqa: E402
from cluster_execd.server import STREAM_LIMIT, handle_connection, peer_uid  # noqa: E402
from cluster_execd.units import Paths  # noqa: E402

from cluster_agent.execd_client import ExecdClient, ExecdError, ExecdLauncher  # noqa: E402
from cluster_agent.executor import ExecRequest, Executor  # noqa: E402

POLICY = {
    "allow_shell": True,
    "allow_as_root_shell": False,
    "root_ops": {"diag.echo": {"argv": ["/bin/echo", "{w}"], "params": {"w": {"enum": ["hi"]}}}},
    "limits_max": {"memory_mb": 256, "cpu_pct": 100, "tasks": 64, "timeout_s": 120},
}


@pytest.fixture
async def execd(tmp_path):
    ctx = Context(
        policy=execd_policy.parse_policy(POLICY),
        paths=Paths(run_root=str(tmp_path / "run"), state_dir=str(tmp_path / "state")),
        run_user=pwd.getpwuid(os.getuid()).pw_name,
        isolation="fallback",
        switch_user=False,
    )
    path = str(tmp_path / "execd.sock")

    async def on_connect(reader, writer):
        uid = peer_uid(writer.get_extra_info("socket"))
        await handle_connection(reader, writer, ctx, uid, [os.getuid()])

    server = await asyncio.start_unix_server(on_connect, path, limit=STREAM_LIMIT)
    yield ctx, ExecdClient(path)
    server.close()


class Sink:
    def __init__(self):
        self.out = []

    async def __call__(self, stream, text):
        self.out.append((stream, text))

    def text(self, stream="stdout"):
        return "".join(t for s, t in self.out if s == stream)


async def test_command_through_execd(execd):
    _, client = execd
    ex = Executor(ExecdLauncher(client), flush_interval=0.05)
    sink = Sink()
    res = await ex.run(ExecRequest("i1", command="echo hi; echo bad >&2; exit 2"), sink)
    assert (res.status, res.exit_code) == ("error", 2)
    assert sink.text() == "hi\n" and sink.text("stderr") == "bad\n"


async def test_root_op_and_policy_rejection(execd):
    _, client = execd
    ex = Executor(ExecdLauncher(client), flush_interval=0.05)
    res = await ex.run(ExecRequest("i2", root_op="diag.echo", params={"w": "rm -rf /"}), Sink())
    assert res.status == "rejected" and "one of" in res.reason
    res = await ex.run(ExecRequest("i3", command="id", as_root=True), Sink())
    assert res.status == "rejected" and "as_root" in res.reason


async def test_cancel_through_execd(execd):
    _, client = execd
    ex = Executor(ExecdLauncher(client), flush_interval=0.05)
    task = asyncio.ensure_future(ex.run(ExecRequest("i4", command="sleep 30"), Sink()))
    for _ in range(100):
        if ex.running():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    assert await ex.cancel("i4")
    res = await asyncio.wait_for(task, 15)
    assert res.status == "cancelled"


async def test_execd_down_is_failed_to_start(tmp_path):
    ex = Executor(ExecdLauncher(ExecdClient(str(tmp_path / "missing.sock"))))
    res = await ex.run(ExecRequest("i5", command="true"), Sink())
    assert res.status == "failed_to_start" and "cluster-execd" in res.reason


async def test_info(execd):
    _, client = execd
    info = await client.info()
    assert info["isolation"] == "fallback" and "diag.echo" in info["policy"]["root_ops"]


async def test_collect_into_outbox(execd, tmp_path):
    ctx, client = execd
    workdir = make_workdir(ctx, "i6")
    os.makedirs(os.path.join(workdir, "out", "sub"))
    with open(os.path.join(workdir, "out", "a.txt"), "w") as f:
        f.write("alpha")
    with open(os.path.join(workdir, "out", "sub", "b.txt"), "w") as f:
        f.write("beta")
    dest = str(tmp_path / "outbox" / "i6")
    result = await client.collect("i6", ["out/*", "out/**/*"], dest)
    names = sorted(f["name"] for f in result["files"])
    assert names == ["out/a.txt", "out/sub/b.txt"]
    with open(os.path.join(dest, "out", "sub", "b.txt")) as f:
        assert f.read() == "beta"
    assert oct(os.stat(os.path.join(dest, "out", "a.txt")).st_mode & 0o777) == "0o600"


async def test_collect_never_follows_symlinks_in_outbox(execd, tmp_path):
    ctx, client = execd
    workdir = make_workdir(ctx, "i7")
    os.makedirs(os.path.join(workdir, "out"))
    with open(os.path.join(workdir, "out", "a.txt"), "w") as f:
        f.write("alpha")
    target = tmp_path / "elsewhere"
    target.mkdir()
    dest = tmp_path / "outbox" / "i7"
    dest.mkdir(parents=True)
    (dest / "out").symlink_to(target)  # planted before collection
    with pytest.raises((ExecdError, OSError)):
        await client.collect("i7", ["out/*"], str(dest))
    assert list(target.iterdir()) == []
