import asyncio
import base64
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cluster_execd.policy import parse_policy  # noqa: E402
from cluster_execd.runner import Context  # noqa: E402
from cluster_execd.server import STREAM_LIMIT, handle_connection, peer_uid  # noqa: E402
from cluster_execd.units import Paths  # noqa: E402

TEST_POLICY = {
    "allow_shell": True,
    "allow_as_root_shell": False,
    "root_ops": {
        "diag.echo": {
            "argv": ["/bin/echo", "{word}"],
            "params": {"word": {"enum": ["hi", "yo"]}},
            "readonly": True,
        },
        "system.reboot": {"argv": ["/usr/bin/systemctl", "reboot"], "detach": True},
        "apt.update": {"argv": ["/usr/bin/apt-get", "update"], "survive_disconnect": True},
    },
    "limits_max": {"memory_mb": 256, "cpu_pct": 200, "tasks": 64, "timeout_s": 600},
    "lan_cidrs": ["192.168.1.0/24"],
}

RUN_USER = "cwtest-run"


@pytest.fixture
def policy():
    return parse_policy(json.loads(json.dumps(TEST_POLICY)))


@pytest.fixture
def paths(tmp_path):
    return Paths(
        run_root=str(tmp_path / "run"),
        agent_root=str(tmp_path / "agent"),
        state_dir=str(tmp_path / "state"),
    )


@pytest.fixture
def ctx(policy, paths):
    """Unprivileged fallback context: runs as the test user, no uid switch."""
    return Context(
        policy=policy,
        paths=paths,
        run_user=pwd.getpwuid(os.getuid()).pw_name,
        isolation="fallback",
        switch_user=False,
    )


@pytest.fixture(scope="session")
def run_user():
    """A real unprivileged account for uid-switch tests (needs root)."""
    if os.geteuid() != 0 or not shutil.which("useradd"):
        pytest.skip("needs root and useradd")
    try:
        pwd.getpwnam(RUN_USER)
        created = False
    except KeyError:
        subprocess.run(
            ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", RUN_USER],
            check=True,
        )
        created = True
    yield RUN_USER
    if created:
        subprocess.run(["userdel", RUN_USER], check=False)


@pytest.fixture
def public_paths():
    """pytest's tmp_path sits in a root-only directory the run user cannot traverse."""
    base = tempfile.mkdtemp(prefix="cwtest-", dir="/var/tmp")
    os.chmod(base, 0o755)
    yield Paths(
        run_root=os.path.join(base, "run"),
        agent_root=os.path.join(base, "agent"),
        state_dir=os.path.join(base, "state"),
    )
    shutil.rmtree(base, ignore_errors=True)


def _world_executable(path):
    path = os.path.realpath(path)
    parts = [path] + [os.path.dirname(path)]
    while parts[-1] not in ("/", ""):
        parts.append(os.path.dirname(parts[-1]))
    return all(os.stat(p).st_mode & 0o001 for p in parts)


@pytest.fixture
def worker_python():
    """The collect worker re-executes this interpreter as the run user."""
    if not _world_executable(sys.executable):
        pytest.skip(f"{sys.executable} is not reachable by other users (test venv layout)")


@pytest.fixture
def root_ctx(policy, public_paths, run_user):
    return Context(
        policy=policy, paths=public_paths, run_user=run_user, isolation="fallback", switch_user=True
    )


class Client:
    """Talks to an in-process execd server the way cluster-agent does."""

    def __init__(self, path):
        self.path = path

    async def open(self, request):
        reader, writer = await asyncio.open_unix_connection(self.path, limit=STREAM_LIMIT)
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        return reader, writer

    async def call(self, request, cancel_after=None, close_after=None, timeout=30):
        reader, writer = await self.open(request)
        events = []

        async def read_all():
            while True:
                line = await reader.readline()
                if not line:
                    return
                events.append(json.loads(line))

        task = asyncio.ensure_future(read_all())
        if cancel_after is not None:
            await asyncio.sleep(cancel_after)
            writer.write(b'{"op":"cancel"}\n')
            await writer.drain()
        if close_after is not None:
            await asyncio.sleep(close_after)
            writer.close()
        await asyncio.wait_for(task, timeout)
        writer.close()
        return events


def output(events, stream="stdout"):
    return b"".join(
        base64.b64decode(e["data"]) for e in events if e["ev"] == "out" and e["stream"] == stream
    ).decode()


def last(events, ev):
    return [e for e in events if e["ev"] == ev][-1]


@pytest.fixture
def make_server(tmp_path):
    servers = []

    async def start(context, allowed=None):
        path = str(tmp_path / f"execd{len(servers)}.sock")
        allowed = [os.getuid()] if allowed is None else allowed

        async def on_connect(reader, writer):
            uid = peer_uid(writer.get_extra_info("socket"))
            await handle_connection(reader, writer, context, uid, allowed)

        server = await asyncio.start_unix_server(on_connect, path, limit=STREAM_LIMIT)
        servers.append(server)
        return Client(path)

    yield start
    for server in servers:
        server.close()
