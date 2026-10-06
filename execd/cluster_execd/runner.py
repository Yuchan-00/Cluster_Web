"""Runs validated plans: as transient systemd units (isolation mode A) or, when systemd-run is
unavailable, as a uid-switched process group with rlimits (mode B, security.md 11.2).
"""

from __future__ import annotations

import asyncio
import logging
import os
import pwd
import shutil
import signal
import stat
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .policy import Policy
from .protocol import EventWriter
from .request import ExecPlan
from .units import Paths, run_argv, stop_argv

log = logging.getLogger(__name__)

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FALLBACK_KILL_GRACE = 10.0
SYSTEMD_BACKSTOP = 15.0  # extra time past RuntimeMaxSec before execd stops the unit itself
RSS_CHECK_INTERVAL = 1.0
RSS_TOLERANCE = 1.1


@dataclass
class Outcome:
    status: str
    exit_code: Optional[int] = None
    reason: Optional[str] = None


@dataclass
class Context:
    policy: Policy
    paths: Paths = field(default_factory=Paths)
    run_user: str = "cluster-run"
    isolation: str = "systemd"  # resolved mode: systemd | fallback
    switch_user: bool = True  # False only in unprivileged tests (fallback runs as ourselves)

    def run_ids(self) -> Tuple[int, int]:
        pw = pwd.getpwnam(self.run_user)
        return pw.pw_uid, pw.pw_gid

    @property
    def runtime_dir(self) -> str:
        return os.path.join(self.paths.state_dir, "runs")


def resolve_isolation(policy: Policy, paths: Paths) -> str:
    systemd_ok = os.path.isdir("/run/systemd/system") and os.access(paths.systemd_run, os.X_OK)
    if policy.isolation == "fallback":
        return "fallback"
    if policy.isolation == "systemd" or systemd_ok:
        return "systemd"
    return "fallback"


def cgroup_controllers() -> List[str]:
    try:
        with open("/sys/fs/cgroup/cgroup.controllers", encoding="ascii") as f:
            return f.read().split()
    except OSError:
        return []


# -- work directories -----------------------------------------------------------------------


def ensure_work_root(ctx: Context) -> None:
    """work/ must be root-owned and not writable by cluster-run, so run dirs cannot be swapped."""
    root = ctx.paths.work_root
    os.makedirs(root, mode=0o755, exist_ok=True)
    st = os.lstat(root)
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeError(f"{root} is not a directory")
    if ctx.switch_user and (st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        os.chown(root, 0, 0)
        os.chmod(root, 0o755)  # noqa: S103 - traversable, writable by root only


def make_workdir(ctx: Context, run_id: str) -> str:
    ensure_work_root(ctx)
    path = ctx.paths.workdir(run_id)
    os.mkdir(path, 0o700)  # fails if it exists: run_ids are never reused
    if ctx.switch_user:
        uid, gid = ctx.run_ids()
        os.chown(path, uid, gid)
    return path


def remove_workdir(ctx: Context, run_id: str) -> bool:
    path = ctx.paths.workdir(run_id)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return True
    # rmtree uses fd-based traversal on Linux, so links planted inside cannot redirect it.
    shutil.rmtree(path, ignore_errors=True)
    return True


# -- helpers ---------------------------------------------------------------------------------


async def _relay(stream: Optional[asyncio.StreamReader], name: str, events: EventWriter) -> None:
    if stream is None:
        return
    while True:
        data = await stream.read(32 * 1024)
        if not data:
            return
        await events.send_bytes("out", data, stream=name)


async def _wait_any(proc_wait: asyncio.Future, cancel: asyncio.Event, timeout: float) -> str:
    """Return "exited", "cancelled" or "timeout", whichever happens first."""
    cancel_wait = asyncio.ensure_future(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            {proc_wait, cancel_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        cancel_wait.cancel()
    if proc_wait in done:
        return "exited"
    return "cancelled" if cancel.is_set() else "timeout"


def _status(rc: Optional[int], why: str, elapsed: float, timeout_s: int) -> str:
    if why == "cancelled":
        return "cancelled"
    if why == "timeout":
        return "timeout"
    if rc == 0:
        return "ok"
    if elapsed >= timeout_s - 0.5:  # killed by RuntimeMaxSec / our timer
        return "timeout"
    return "error"


# -- mode A: systemd -------------------------------------------------------------------------


async def run_systemd(
    plan: ExecPlan, ctx: Context, events: EventWriter, cancel: asyncio.Event
) -> Outcome:
    argv = run_argv(plan, ctx.paths, ctx.run_user, list(ctx.policy.lan_cidrs))
    env = {"PATH": SAFE_PATH, "LANG": "C.UTF-8"}
    if plan.detach:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await proc.communicate()
        if proc.returncode == 0:
            return Outcome("scheduled", 0)
        return Outcome("failed_to_start", proc.returncode, err.decode("utf-8", "replace")[-500:])

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    relays = [
        asyncio.ensure_future(_relay(proc.stdout, "stdout", events)),
        asyncio.ensure_future(_relay(proc.stderr, "stderr", events)),
    ]
    proc_wait = asyncio.ensure_future(proc.wait())
    why = await _wait_any(proc_wait, cancel, plan.limits.timeout_s + SYSTEMD_BACKSTOP)
    if why != "exited":
        await stop_unit(plan.run_id, ctx)
        await proc_wait
    await asyncio.wait(relays, timeout=5)
    for task in relays:
        task.cancel()
    rc = proc.returncode
    return Outcome(_status(rc, why, time.monotonic() - started, plan.limits.timeout_s), rc)


async def stop_unit(run_id: str, ctx: Context) -> None:
    proc = await asyncio.create_subprocess_exec(
        *stop_argv(run_id, ctx.paths),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": SAFE_PATH},
    )
    await proc.wait()


# -- mode B: fallback ------------------------------------------------------------------------


def fallback_argv(plan: ExecPlan, ctx: Context) -> List[str]:
    """Exec chain: oom score -> rlimits -> drop to cluster-run -> the command.

    util-linux tools do the privileged steps so no Python code runs between fork and exec.
    """
    lim = plan.limits
    argv: List[str] = []
    if shutil.which("choom"):  # util-linux >= 2.33; OOM preference is best effort
        argv += ["choom", "-n", "500", "--"]
    argv += ["prlimit", f"--nproc={lim.tasks}", f"--as={lim.memory_mb * 2 * 1024 * 1024}", "--"]
    if ctx.switch_user and not plan.as_root:
        argv += [
            "setpriv",
            f"--reuid={ctx.run_user}",
            f"--regid={ctx.run_user}",
            "--clear-groups",
            "--no-new-privs",
            "--",
        ]
    return argv + plan.argv


def _proc_start_time(pid: int) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
            return f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _group_rss_mb(pgid: int) -> float:
    total = 0
    page = os.sysconf("SC_PAGE_SIZE")
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="ascii", errors="replace") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            if int(fields[2]) == pgid:  # field 5 (pgrp), counted after the comm field
                total += int(fields[21]) * page  # field 24 (rss pages)
        except (OSError, ValueError, IndexError):
            continue
    return total / (1024 * 1024)


async def _watch_rss(pgid: int, limit_mb: int, oom: asyncio.Event) -> None:
    over = 0
    while True:
        await asyncio.sleep(RSS_CHECK_INTERVAL)
        over = over + 1 if _group_rss_mb(pgid) > limit_mb * RSS_TOLERANCE else 0
        if over >= 2:
            oom.set()
            return


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


async def run_fallback(
    plan: ExecPlan, ctx: Context, events: EventWriter, cancel: asyncio.Event
) -> Outcome:
    if plan.detach:
        return Outcome("failed_to_start", reason="detached root_ops need systemd")
    workdir = None if plan.as_root else ctx.paths.workdir(plan.run_id)
    env = dict(plan.env)
    env["PATH"] = SAFE_PATH
    env["HOME"] = workdir or "/root"
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *fallback_argv(plan, ctx),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir or "/",
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return Outcome("failed_to_start", reason=str(exc))
    record_fallback_run(ctx, plan.run_id, proc.pid)
    oom = asyncio.Event()
    tasks = [
        asyncio.ensure_future(_relay(proc.stdout, "stdout", events)),
        asyncio.ensure_future(_relay(proc.stderr, "stderr", events)),
        asyncio.ensure_future(_watch_rss(proc.pid, plan.limits.memory_mb, oom)),
    ]
    proc_wait = asyncio.ensure_future(proc.wait())
    stop = asyncio.Event()

    async def _either() -> None:
        oom_wait = asyncio.ensure_future(oom.wait())
        cancel_wait = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait({oom_wait, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            oom_wait.cancel()
            cancel_wait.cancel()
        stop.set()

    either = asyncio.ensure_future(_either())
    try:
        why = await _wait_any(proc_wait, stop, plan.limits.timeout_s)
        if why != "exited":
            _killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.shield(proc_wait), FALLBACK_KILL_GRACE)
            except asyncio.TimeoutError:
                _killpg(proc.pid, signal.SIGKILL)
                await proc_wait
        # Children that outlived the leader are part of the run too.
        _killpg(proc.pid, signal.SIGKILL)
        await asyncio.wait(tasks[:2], timeout=5)
    finally:
        either.cancel()
        for task in tasks:
            task.cancel()
        forget_fallback_run(ctx, plan.run_id)
    rc = proc.returncode
    if oom.is_set():
        return Outcome("oom", rc, "memory limit exceeded")
    status = _status(
        rc,
        "cancelled" if cancel.is_set() else why,
        time.monotonic() - started,
        plan.limits.timeout_s,
    )
    return Outcome(status, rc)


def record_fallback_run(ctx: Context, run_id: str, pid: int) -> None:
    """Remember the process group so a later kind=stop from another connection can find it."""
    os.makedirs(ctx.runtime_dir, mode=0o700, exist_ok=True)
    path = os.path.join(ctx.runtime_dir, run_id)
    with open(path, "w", encoding="ascii") as f:
        f.write(f"{pid} {_proc_start_time(pid)}\n")


def forget_fallback_run(ctx: Context, run_id: str) -> None:
    try:
        os.unlink(os.path.join(ctx.runtime_dir, run_id))
    except FileNotFoundError:
        pass


def stop_fallback_run(ctx: Context, run_id: str) -> bool:
    try:
        with open(os.path.join(ctx.runtime_dir, run_id), encoding="ascii") as f:
            pid_s, start = f.read().split()
    except (OSError, ValueError):
        return False
    pid = int(pid_s)
    if _proc_start_time(pid) != start:  # pid was reused by an unrelated process
        return False
    _killpg(pid, signal.SIGTERM)
    return True


# -- entry point -----------------------------------------------------------------------------


async def run_exec(
    plan: ExecPlan, ctx: Context, events: EventWriter, cancel: asyncio.Event
) -> Outcome:
    if ctx.isolation == "fallback" and plan.as_root and not plan.root_op:
        log.warning("as_root shell on a fallback node: no sandbox, rlimits only")
    if plan.sandboxed:
        try:
            make_workdir(ctx, plan.run_id)
        except FileExistsError:
            return Outcome("failed_to_start", reason="duplicate run_id")
    try:
        if ctx.isolation == "systemd":
            return await run_systemd(plan, ctx, events, cancel)
        return await run_fallback(plan, ctx, events, cancel)
    finally:
        if plan.sandboxed:
            remove_workdir(ctx, plan.run_id)  # commands keep nothing; jobs collect first
