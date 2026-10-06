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
from typing import Any, Dict, List, Optional, Tuple

from .policy import Policy
from .protocol import ClientGone, EventWriter
from .request import ExecPlan
from .units import Paths, run_argv, stop_argv, unit_name

log = logging.getLogger(__name__)

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FALLBACK_KILL_GRACE = 10.0
SYSTEMD_BACKSTOP = 15.0  # extra time past RuntimeMaxSec before execd stops the unit itself
STOP_RETRIES = 30
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


class _Tail:
    """Keeps the last bytes of a stream (systemd-run's own errors arrive on stderr)."""

    def __init__(self, size: int = 2048) -> None:
        self.size = size
        self.data = b""

    def add(self, chunk: bytes) -> None:
        self.data = (self.data + chunk)[-self.size :]

    def text(self) -> str:
        return self.data.decode("utf-8", "replace").strip()


async def _relay(
    stream: Optional[asyncio.StreamReader],
    name: str,
    events: EventWriter,
    tail: Optional[_Tail] = None,
) -> None:
    """Forward a pipe to the agent. Keeps draining after the agent is gone: an undrained pipe
    blocks the child, and on Python 3.11/3.12.0-3.12.3 Process.wait() also waits for EOF."""
    if stream is None:
        return
    gone = False
    while True:
        data = await stream.read(32 * 1024)
        if not data:
            return
        if tail is not None:
            tail.add(data)
        if gone:
            continue
        try:
            await events.send_bytes("out", data, stream=name)
        except ClientGone:
            gone = True


async def _finish(tasks: List[asyncio.Future], timeout: float = 5.0) -> None:
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
    for task in tasks:
        task.cancel()
        if task.done() and not task.cancelled() and task.exception() is not None:
            log.warning("relay failed: %s", task.exception())


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


def _first_of(*events: asyncio.Event) -> Tuple[asyncio.Event, asyncio.Future]:
    """An event set as soon as any of the given events is set (plus the task to cancel)."""
    combined = asyncio.Event()

    async def _watch() -> None:
        waits = [asyncio.ensure_future(e.wait()) for e in events]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
        combined.set()

    return combined, asyncio.ensure_future(_watch())


def _status(rc: Optional[int], why: str, elapsed: float, timeout_s: int) -> str:
    if why == "cancelled":
        return "cancelled"
    if why == "timeout":
        return "timeout"
    if rc == 0:
        return "ok"
    if elapsed >= timeout_s - 0.5:  # killed by our timer
        return "timeout"
    return "error"


async def _run_quiet(argv: List[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": SAFE_PATH, "LANG": "C.UTF-8"},
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace")


def _pid_rss_bytes(pid: str) -> int:
    try:
        with open(f"/proc/{pid}/statm", encoding="ascii") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


# -- mode A: systemd -------------------------------------------------------------------------


async def query_unit(run_id: str, ctx: Context) -> Dict[str, str]:
    out = await _run_quiet(
        [
            ctx.paths.systemctl,
            "show",
            "-p",
            "LoadState",
            "-p",
            "Result",
            "-p",
            "ExecMainCode",
            "-p",
            "ExecMainStatus",
            "-p",
            "ControlGroup",
            "--",
            unit_name(run_id),
        ]
    )
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def unit_outcome(rc: Optional[int], why: str, unit: Dict[str, str], stderr: str) -> Outcome:
    """Map the unit's result (security.md 11.2): systemd-run --wait folds oom-kill and timeout
    into exit status 1 and signals into 255, so its return code alone cannot tell them apart."""
    if why == "cancelled":
        return Outcome("cancelled", rc)
    if why == "timeout":
        return Outcome("timeout", rc, "execd stopped a unit that outlived RuntimeMaxSec")
    result = unit.get("Result", "")
    if unit.get("LoadState") == "loaded" and result:
        status = unit.get("ExecMainStatus", "")
        code = int(status) if status.isdigit() else rc
        if result == "success":
            return Outcome("ok", 0)
        if result == "exit-code":
            return Outcome("error", code)
        if result == "oom-kill":
            return Outcome("oom", rc, "killed for exceeding MemoryMax")
        if result == "timeout":
            return Outcome("timeout", rc)
        if result in ("signal", "core-dump"):
            return Outcome("error", None, f"killed by signal {status}")
        if result == "resources":
            return Outcome("failed_to_start", rc, stderr[-500:] or "unit setup failed")
        return Outcome("error", rc, f"unit result {result}")
    # Successful units are garbage-collected at once; failed ones stay loaded until
    # reset-failed. Gone + non-zero therefore means systemd-run never created the unit.
    if rc == 0:
        return Outcome("ok", 0)
    return Outcome("failed_to_start", rc, stderr[-500:] or "systemd-run failed")


async def _unit_rss_mb(run_id: str, ctx: Context, cgroup: List[str]) -> float:
    if not cgroup:
        value = (await query_unit(run_id, ctx)).get("ControlGroup", "")
        if not value:
            return 0.0
        cgroup.append(value)
    for root in ("/sys/fs/cgroup", "/sys/fs/cgroup/unified", "/sys/fs/cgroup/systemd"):
        try:
            with open(f"{root}{cgroup[0]}/cgroup.procs", encoding="ascii") as f:
                pids = f.read().split()
        except OSError:
            continue
        return sum(_pid_rss_bytes(p) for p in pids) / (1024 * 1024)
    return 0.0


async def _watch_unit_rss(run_id: str, ctx: Context, limit_mb: int, oom: asyncio.Event) -> None:
    """Mode A without the memory controller ignores MemoryMax silently (common on Pi kernels):
    measure the unit's cgroup ourselves (security.md 11.2, jobs.md 9.2)."""
    cgroup: List[str] = []
    over = 0
    while True:
        await asyncio.sleep(RSS_CHECK_INTERVAL)
        rss = await _unit_rss_mb(run_id, ctx, cgroup)
        over = over + 1 if rss > limit_mb * RSS_TOLERANCE else 0
        if over >= 2:
            oom.set()
            return


async def _stop_until_exit(run_id: str, ctx: Context, proc: Any, proc_wait: Any) -> None:
    """A cancel can arrive before PID 1 has created the unit, when stop is a no-op: retry."""
    for _ in range(STOP_RETRIES):
        await stop_unit(run_id, ctx)
        try:
            await asyncio.wait_for(asyncio.shield(proc_wait), 1.0)
            return
        except asyncio.TimeoutError:
            continue
    log.error("unit %s did not stop; killing systemd-run", unit_name(run_id))
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(asyncio.shield(proc_wait), 5.0)
    except asyncio.TimeoutError:
        log.error("systemd-run for %s did not exit", run_id)


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

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stderr_tail = _Tail()
    relays = [
        asyncio.ensure_future(_relay(proc.stdout, "stdout", events)),
        asyncio.ensure_future(_relay(proc.stderr, "stderr", events, stderr_tail)),
    ]
    oom = asyncio.Event()
    watchers: List[asyncio.Future] = []
    if "memory" not in cgroup_controllers():
        watchers.append(
            asyncio.ensure_future(_watch_unit_rss(plan.run_id, ctx, plan.limits.memory_mb, oom))
        )
    stop, stop_task = _first_of(cancel, oom)
    watchers.append(stop_task)
    proc_wait = asyncio.ensure_future(proc.wait())
    try:
        why = await _wait_any(proc_wait, stop, plan.limits.timeout_s + SYSTEMD_BACKSTOP)
        if why != "exited":
            await _stop_until_exit(plan.run_id, ctx, proc, proc_wait)
        await _finish(relays)
        unit = await query_unit(plan.run_id, ctx)
        await _run_quiet([ctx.paths.systemctl, "reset-failed", "--", unit_name(plan.run_id)])
    finally:
        for task in watchers:
            task.cancel()
    if oom.is_set():
        return Outcome("oom", proc.returncode, "memory limit exceeded (RSS watch)")
    if why == "cancelled" and not cancel.is_set():
        why = "exited"
    return unit_outcome(proc.returncode, why, unit, stderr_tail.text())


async def stop_unit(run_id: str, ctx: Context) -> None:
    await _run_quiet(stop_argv(run_id, ctx.paths))


# -- mode B: fallback ------------------------------------------------------------------------


def fallback_argv(plan: ExecPlan, ctx: Context) -> List[str]:
    """Exec chain: oom score -> rlimits -> drop to cluster-run -> the command.

    util-linux tools do the privileged steps so no Python code runs between fork and exec.
    """
    lim = plan.limits
    argv: List[str] = []
    if shutil.which("choom"):  # util-linux >= 2.33; OOM preference is best effort
        argv += ["choom", "-n", "500", "--"]
    limits = [f"--as={lim.memory_mb * 2 * 1024 * 1024}"]
    if ctx.switch_user and not plan.as_root:
        # RLIMIT_NPROC counts every process of the account, so it only works as a per-run
        # bound for the dedicated cluster-run account (shared by its concurrent runs).
        limits.insert(0, f"--nproc={lim.tasks}")
    argv += ["prlimit"] + limits + ["--"]
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
                try:
                    await asyncio.wait_for(asyncio.shield(proc_wait), FALLBACK_KILL_GRACE)
                except asyncio.TimeoutError:
                    log.error("run %s survived SIGKILL", plan.run_id)
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
