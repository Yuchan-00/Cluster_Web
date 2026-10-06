import asyncio
import time

import pytest

from cluster_agent.executor import DirectLauncher, ExecRequest, Executor


class Sink:
    def __init__(self):
        self.chunks = []
        self.started = time.monotonic()

    async def __call__(self, stream, text):
        self.chunks.append((time.monotonic() - self.started, stream, text))

    def text(self, stream="stdout"):
        return "".join(t for _, s, t in self.chunks if s == stream)


def make_executor(**kw):
    kw.setdefault("kill_grace", 1.0)
    kw.setdefault("flush_interval", 0.05)
    return Executor(DirectLauncher(), **kw)


async def test_success_and_streams():
    sink = Sink()
    res = await make_executor().run(ExecRequest("r1", command="echo out; echo err >&2"), sink)
    assert res.status == "ok" and res.exit_code == 0
    assert sink.text("stdout") == "out\n"
    assert sink.text("stderr") == "err\n"


async def test_nonzero_exit_is_error():
    res = await make_executor().run(ExecRequest("r1", command="exit 3"), Sink())
    assert res.status == "error" and res.exit_code == 3


async def test_argv_mode_does_not_use_a_shell():
    sink = Sink()
    res = await make_executor().run(ExecRequest("r1", argv=["echo", "$HOME", ";", "id"]), sink)
    assert res.status == "ok"
    assert sink.text() == "$HOME ; id\n"


async def test_timeout_kills_whole_process_group(tmp_path):
    pidfile = tmp_path / "bg.pid"
    cmd = f"sleep 30 & echo $! > {pidfile}; sleep 30"
    started = time.monotonic()
    res = await make_executor().run(ExecRequest("r1", command=cmd, timeout=0.5), Sink())
    assert res.status == "timeout"
    assert time.monotonic() - started < 5
    bg_pid = int(pidfile.read_text())
    await asyncio.sleep(0.1)
    assert not _alive(bg_pid)


def _alive(pid):
    """True if pid is running. A zombie (killed, not yet reaped by init) counts as dead."""
    try:
        with open(f"/proc/{pid}/status") as f:
            state = next(line for line in f if line.startswith("State:"))
    except FileNotFoundError:
        return False
    return "Z" not in state.split()[1]


async def test_cancel():
    ex = make_executor()
    task = asyncio.ensure_future(ex.run(ExecRequest("r1", command="sleep 30"), Sink()))
    for _ in range(50):
        if ex.running():
            break
        await asyncio.sleep(0.02)
    assert await ex.cancel("r1") is True
    res = await asyncio.wait_for(task, 5)
    assert res.status == "cancelled"
    assert await ex.cancel("r1") is False


async def test_output_cap_truncates_but_drains():
    sink = Sink()
    ex = make_executor(max_output_bytes=1000)
    res = await ex.run(ExecRequest("r1", command="head -c 200000 /dev/zero | tr '\\0' 'a'"), sink)
    assert res.status == "ok"
    assert res.truncated is True
    forwarded = sink.text()
    assert forwarded.split("\n")[0] == "a" * 1000
    assert "output limit reached" in forwarded


async def test_busy_and_duplicate_rejected():
    ex = make_executor(max_concurrent=1)
    task = asyncio.ensure_future(ex.run(ExecRequest("r1", command="sleep 30"), Sink()))
    for _ in range(50):
        if ex.running():
            break
        await asyncio.sleep(0.02)
    dup = await ex.run(ExecRequest("r1", command="true"), Sink())
    assert dup.status == "rejected" and dup.reason == "duplicate run_id"
    busy = await ex.run(ExecRequest("r2", command="true"), Sink())
    assert busy.status == "rejected" and busy.reason == "busy"
    await ex.cancel_all()
    await asyncio.wait_for(task, 5)


@pytest.mark.parametrize(
    "req",
    [
        ExecRequest("r", command="true", argv=["true"]),
        ExecRequest("r"),
        ExecRequest("r", argv=[]),
        ExecRequest("r", command="   "),
        ExecRequest("r", command="echo \x00"),
        ExecRequest("r", command="true", timeout=0),
        ExecRequest("r", command="true", as_root=True),
    ],
)
async def test_invalid_requests_rejected(req):
    res = await make_executor().run(req, Sink())
    assert res.status == "rejected"


async def test_agent_environment_not_inherited(monkeypatch):
    monkeypatch.setenv("CLUSTER_AGENT_SECRET", "do-not-leak")
    sink = Sink()
    await make_executor().run(ExecRequest("r1", command="env"), sink)
    assert "do-not-leak" not in sink.text()
    assert "PATH=" in sink.text()


async def test_quiet_process_output_flushed_before_exit():
    sink = Sink()
    await make_executor().run(ExecRequest("r1", command="echo first; sleep 1; echo second"), sink)
    first = next(t for t, s, text in sink.chunks if "first" in text)
    assert first < 0.8


async def test_multibyte_utf8_split_across_reads():
    sink = Sink()
    cmd = "python3 -c \"import sys; sys.stdout.write('한글' * 3000)\""
    res = await make_executor(max_output_bytes=10**6).run(ExecRequest("r1", command=cmd), sink)
    assert res.status == "ok"
    assert sink.text() == "한글" * 3000


async def test_missing_cwd_fails_to_start():
    res = await make_executor().run(ExecRequest("r1", command="true", cwd="/nonexistent/x"), Sink())
    assert res.status == "failed_to_start"
