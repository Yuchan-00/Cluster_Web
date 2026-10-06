"""Runs commands for the master: concurrency, output batching and caps, cancellation.

Where and how a run actually executes is the Launcher's job: ExecdLauncher (execd_client.py)
delegates to cluster-execd on real nodes; DirectLauncher runs a local process group and exists
for mock clusters and tests.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# (stream, text) -> None. stream is "stdout" or "stderr".
OutputSink = Callable[[str, str], Awaitable[None]]

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# Output chunks buffered per run. Bounded so that a stalled master link pushes back on the
# process (its pipe fills and it blocks) instead of growing the agent's memory (MemoryMax=80M).
OUTPUT_QUEUE_CHUNKS = 16
TRUNCATION_NOTICE = "\n[cluster-agent] output limit reached; further output discarded\n"
NETWORKS = ("internet", "lan", "none")
_LIMIT_KEYS = ("memory_mb", "cpu_pct", "tasks", "timeout_s")


class LaunchRejected(Exception):
    """The run was refused (policy, validation, busy): nothing was started."""

    status = "rejected"


class LaunchFailed(LaunchRejected):
    """The run was accepted but its process could not be started."""

    status = "failed_to_start"


@dataclass
class ExecRequest:
    run_id: str
    command: Optional[str] = None  # shell mode: /bin/sh -c <command>
    argv: Optional[List[str]] = None  # preset mode: executed directly, no shell
    root_op: Optional[str] = None  # node-policy operation id; argv comes from the node
    params: Dict[str, Any] = field(default_factory=dict)
    as_root: bool = False
    timeout: float = 60.0
    limits: Dict[str, int] = field(default_factory=dict)
    network: str = "internet"
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "root_op" if self.root_op is not None else "command"

    def validate(self) -> Optional[str]:
        """Shape checks only; cluster-execd does the authoritative validation."""
        given = [x is not None for x in (self.command, self.argv, self.root_op)]
        if sum(given) != 1:
            return "exactly one of command, argv or root_op is required"
        if self.argv is not None and not (
            isinstance(self.argv, list) and self.argv and all(isinstance(a, str) for a in self.argv)
        ):
            return "argv must be a non-empty list of strings"
        if self.command is not None and (
            not isinstance(self.command, str) or not self.command.strip()
        ):
            return "command is empty"
        if self.root_op is not None and not isinstance(self.root_op, str):
            return "root_op must be a string"
        if "\x00" in (self.command or "") or any("\x00" in a for a in self.argv or []):
            return "NUL byte in command"
        if not 0 < self.timeout <= 24 * 3600:
            return "timeout out of range"
        if self.network not in NETWORKS:
            return "invalid network mode"
        if not isinstance(self.limits, dict) or set(self.limits) - set(_LIMIT_KEYS):
            return "invalid limits"
        if not isinstance(self.env, dict) or not isinstance(self.params, dict):
            return "env and params must be objects"
        return None

    @classmethod
    def from_message(cls, msg: Dict[str, Any], default_timeout: float = 60.0) -> ExecRequest:
        """Build a request from the master's `exec` message (docs/PLAN.md 12.1)."""
        limits = msg.get("limits") or {}
        if not isinstance(limits, dict):
            raise ValueError("limits must be an object")
        timeout = msg.get("timeout", limits.get("timeout_s", default_timeout))
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise ValueError("timeout must be a number")
        try:
            timeout = float(timeout)
        except OverflowError as exc:
            raise ValueError("timeout out of range") from exc
        if timeout != timeout:  # NaN
            raise ValueError("timeout out of range")
        mode = msg.get("mode")
        root_op = msg.get("root_op")
        if root_op is None and mode not in ("shell", "preset"):
            raise ValueError("mode must be shell or preset")
        return cls(
            run_id=str(msg.get("run_id", "")),
            command=msg.get("command") if root_op is None and mode == "shell" else None,
            argv=msg.get("argv") if root_op is None and mode == "preset" else None,
            root_op=root_op,
            params=msg.get("params") or {},
            as_root=msg.get("as_root") is True,
            timeout=timeout,
            limits={k: v for k, v in limits.items() if k != "timeout_s"},
            network=msg.get("network", "internet"),
            env=msg.get("env") or {},
        )

    def to_execd(self) -> Dict[str, Any]:
        limits = dict(self.limits)
        limits["timeout_s"] = max(1, int(round(self.timeout)))
        req: Dict[str, Any] = {
            "v": 1,
            "run_id": self.run_id,
            "kind": self.kind,
            "limits": limits,
            "network": self.network,
            "env": self.env,
        }
        if self.root_op is not None:
            req.update(root_op=self.root_op, params=self.params)
        elif self.command is not None:
            req.update(mode="shell", command=self.command, as_root=self.as_root)
        else:
            req.update(mode="preset", argv=self.argv, as_root=self.as_root)
        return req


@dataclass
class ExecResult:
    run_id: str
    status: str  # ok | error | timeout | cancelled | oom | scheduled | rejected | failed_to_start
    exit_code: Optional[int] = None
    duration_ms: int = 0
    output_bytes: int = 0
    truncated: bool = False
    reason: Optional[str] = None
    dropped_bytes: int = 0

    def to_message(self) -> Dict[str, Any]:
        msg: Dict[str, Any] = {
            "type": "cmd_result",
            "run_id": self.run_id,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_bytes": self.output_bytes,
            "truncated": self.truncated,
        }
        if self.reason:
            msg["reason"] = self.reason
        if self.dropped_bytes:
            msg["dropped_bytes"] = self.dropped_bytes
        return msg


class RunHandle:
    """A started run: yields output chunks, then reports how it ended."""

    def output(self) -> AsyncIterator[Tuple[str, bytes]]:
        raise NotImplementedError

    async def result(self) -> Tuple[str, Optional[int], Optional[str]]:
        """(status, exit_code, reason) once the run is over and output is drained."""
        raise NotImplementedError

    async def cancel(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        """Release resources (connections, pipes). Called exactly once by the Executor."""


class Launcher:
    async def start(self, req: ExecRequest) -> RunHandle:
        raise NotImplementedError


# -- direct launcher (development and tests) -------------------------------------------------


class DirectLauncher(Launcher):
    """Runs as the agent's own account in a new process group. No isolation: dev/mock only."""

    def __init__(self, kill_grace: float = 5.0) -> None:
        self.kill_grace = kill_grace

    async def start(self, req: ExecRequest) -> RunHandle:
        if req.as_root or req.root_op is not None:
            raise LaunchRejected("as_root and root_op need cluster-execd")
        argv = list(req.argv) if req.argv is not None else ["/bin/sh", "-c", req.command or ""]
        env = {"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        env.update(req.env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            raise LaunchFailed(str(exc)) from exc
        return _DirectRun(proc, req.timeout, self.kill_grace)


class _DirectRun(RunHandle):
    def __init__(self, proc: asyncio.subprocess.Process, timeout: float, grace: float) -> None:
        self.proc = proc
        self.grace = grace
        self.queue: asyncio.Queue[Optional[Tuple[str, bytes]]] = asyncio.Queue(OUTPUT_QUEUE_CHUNKS)
        self.cancelled = False
        self.timed_out = False
        self.readers = [
            asyncio.ensure_future(self._read("stdout", proc.stdout)),
            asyncio.ensure_future(self._read("stderr", proc.stderr)),
        ]
        self.done = asyncio.ensure_future(self._supervise(timeout))

    async def _read(self, name: str, stream: Optional[asyncio.StreamReader]) -> None:
        if stream is None:
            return
        while True:
            data = await stream.read(4096)
            if not data:
                return
            await self.queue.put((name, data))

    async def _supervise(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.timed_out = True
            await self._terminate()
        # Grandchildren that kept the pipes open must not hang the result forever.
        await asyncio.wait(self.readers, timeout=self.grace)
        for task in self.readers:
            task.cancel()
        await self.queue.put(None)

    async def _terminate(self) -> None:
        if self.proc.returncode is None:
            self._signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=self.grace)
            except asyncio.TimeoutError:
                self._signal(signal.SIGKILL)
                await self.proc.wait()
        self._signal(signal.SIGKILL)  # leftovers in the group

    def _signal(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log.warning("cannot signal process group %s: %s", self.proc.pid, exc)

    async def output(self) -> AsyncIterator[Tuple[str, bytes]]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def result(self) -> Tuple[str, Optional[int], Optional[str]]:
        await asyncio.shield(self.done)
        rc = self.proc.returncode
        if self.cancelled:
            return "cancelled", rc, None
        if self.timed_out:
            return "timeout", rc, None
        return ("ok" if rc == 0 else "error"), rc, None

    async def cancel(self) -> None:
        self.cancelled = True
        await self._terminate()

    async def close(self) -> None:
        if self.proc.returncode is None:
            await self._terminate()
        self.done.cancel()


# -- executor --------------------------------------------------------------------------------


class Executor:
    """Bookkeeping around launchers: limits, output pumping, cancellation, result shaping."""

    BACKSTOP_S = 30.0  # launchers enforce timeouts; this only catches a hung launcher

    def __init__(
        self,
        launcher: Launcher,
        max_concurrent: int = 2,
        max_output_bytes: int = 1024 * 1024,
        flush_interval: float = 0.2,
        flush_bytes: int = 8192,
    ) -> None:
        self.launcher = launcher
        self.max_concurrent = max_concurrent
        self.max_output_bytes = max_output_bytes
        self.flush_interval = flush_interval
        self.flush_bytes = flush_bytes
        self._handles: Dict[str, RunHandle] = {}
        self._reserved: Dict[str, bool] = {}
        self._cancelled: Dict[str, bool] = {}

    def running(self) -> List[str]:
        """Runs being started or running (both must be visible to cancel and to hello)."""
        return list(self._reserved)

    async def cancel(self, run_id: str) -> bool:
        if run_id not in self._reserved:
            return False
        self._cancelled[run_id] = True  # honoured right after start if still starting
        handle = self._handles.get(run_id)
        if handle is not None:
            await handle.cancel()
        return True

    async def cancel_all(self) -> None:
        await asyncio.gather(*(self.cancel(r) for r in list(self._reserved)))

    async def run(self, req: ExecRequest, sink: OutputSink) -> ExecResult:
        started = time.monotonic()
        problem = req.validate()
        if problem:
            return ExecResult(req.run_id, "rejected", reason=problem)
        if req.run_id in self._reserved:
            return ExecResult(req.run_id, "rejected", reason="duplicate run_id")
        if len(self._reserved) >= self.max_concurrent:
            return ExecResult(req.run_id, "rejected", reason="busy")

        self._reserved[req.run_id] = True
        try:
            try:
                handle = await self.launcher.start(req)
            except LaunchRejected as exc:
                return ExecResult(req.run_id, exc.status, reason=str(exc), duration_ms=_ms(started))
            return await self._drive(req, handle, sink, started)
        finally:
            self._reserved.pop(req.run_id, None)
            self._cancelled.pop(req.run_id, None)

    async def _drive(
        self, req: ExecRequest, handle: RunHandle, sink: OutputSink, started: float
    ) -> ExecResult:
        self._handles[req.run_id] = handle
        if self._cancelled.get(req.run_id):  # cancel or lockdown arrived while starting
            await handle.cancel()
        out = _OutputPump(sink, self.max_output_bytes, self.flush_interval, self.flush_bytes)
        feeder = asyncio.ensure_future(_feed(handle, out))
        ticker = asyncio.ensure_future(out.tick())
        try:
            try:
                status, code, reason = await asyncio.wait_for(
                    handle.result(), timeout=req.timeout + self.BACKSTOP_S
                )
            except asyncio.TimeoutError:
                await handle.cancel()
                status, code, reason = "timeout", None, "launcher did not finish in time"
            await asyncio.wait([feeder], timeout=5)
        finally:
            for task in (feeder, ticker):
                task.cancel()
            self._handles.pop(req.run_id, None)
            cancelled = self._cancelled.pop(req.run_id, False)
            await handle.close()
        await out.flush()
        if cancelled and status not in ("ok", "scheduled"):
            status = "cancelled"
        return ExecResult(
            req.run_id,
            status,
            exit_code=code,
            duration_ms=_ms(started),
            output_bytes=out.total,
            truncated=out.truncated,
            reason=reason,
        )


async def _feed(handle: RunHandle, out: _OutputPump) -> None:
    decoders = {
        s: codecs.getincrementaldecoder("utf-8")(errors="replace") for s in ("stdout", "stderr")
    }
    async for stream, data in handle.output():
        await out.add(stream, decoders[stream].decode(data), len(data))
    for stream, decoder in decoders.items():
        tail = decoder.decode(b"", final=True)
        if tail:
            await out.add(stream, tail, 0)


class _OutputPump:
    """Batches output into chunks and stops forwarding (but keeps draining) past the cap."""

    def __init__(self, sink: OutputSink, limit: int, interval: float, max_chunk: int) -> None:
        self.sink = sink
        self.limit = limit
        self.interval = interval
        self.max_chunk = max_chunk
        self.total = 0
        self.truncated = False
        self._buffers: Dict[str, List[str]] = {"stdout": [], "stderr": []}
        self._sizes: Dict[str, int] = {"stdout": 0, "stderr": 0}
        self._last_flush = time.monotonic()
        self._lock = asyncio.Lock()

    async def add(self, stream: str, text: str, nbytes: int) -> None:
        async with self._lock:
            if self.truncated:
                return
            if self.total + nbytes > self.limit:
                keep = max(0, self.limit - self.total)
                text = text.encode("utf-8")[:keep].decode("utf-8", "ignore") + TRUNCATION_NOTICE
                self.truncated = True
            self.total += nbytes
            self._buffers[stream].append(text)
            self._sizes[stream] += len(text)
            due = time.monotonic() - self._last_flush >= self.interval
            if self.truncated or due or self._sizes[stream] >= self.max_chunk:
                await self._flush_locked()

    async def tick(self) -> None:
        """Flush periodically so a quiet process's last line still reaches the user promptly."""
        while True:
            await asyncio.sleep(self.interval)
            await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        self._last_flush = time.monotonic()
        for stream in ("stdout", "stderr"):
            if self._buffers[stream]:
                text = "".join(self._buffers[stream])
                self._buffers[stream] = []
                self._sizes[stream] = 0
                try:
                    await self.sink(stream, text)
                except Exception:  # noqa: BLE001 - a broken sink must not kill the run
                    log.exception("output sink failed")


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
