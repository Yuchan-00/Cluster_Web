"""Runs commands for the master: streams output, enforces timeouts, output caps and cancellation.

How a process is started under another account (cluster-run, or root for as_root) is the
Launcher's job; the Executor only deals with process lifetime and output.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# (stream, text) -> None. stream is "stdout" or "stderr".
OutputSink = Callable[[str, str], Awaitable[None]]

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TRUNCATION_NOTICE = "\n[cluster-agent] output limit reached; further output discarded\n"


@dataclass
class ExecRequest:
    run_id: str
    command: Optional[str] = None  # shell mode: passed to /bin/sh -c
    argv: Optional[List[str]] = None  # preset mode: executed directly, no shell
    timeout: float = 60.0
    as_root: bool = False
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None

    def validate(self) -> Optional[str]:
        """Return an error message, or None if the request is well formed."""
        if (self.command is None) == (self.argv is None):
            return "exactly one of command or argv is required"
        if self.argv is not None and not (self.argv and all(isinstance(a, str) for a in self.argv)):
            return "argv must be a non-empty list of strings"
        if self.command is not None and not self.command.strip():
            return "command is empty"
        if "\x00" in (self.command or "") or any("\x00" in a for a in self.argv or []):
            return "NUL byte in command"
        if not 0 < self.timeout <= 24 * 3600:
            return "timeout out of range"
        return None


@dataclass
class ExecResult:
    run_id: str
    status: str  # ok | error | timeout | cancelled | rejected | failed_to_start
    exit_code: Optional[int] = None
    duration_ms: int = 0
    output_bytes: int = 0
    truncated: bool = False
    reason: Optional[str] = None


class Launcher:
    """Turns a request into a spawned process and knows how to signal everything it started."""

    def argv(self, req: ExecRequest) -> List[str]:
        raise NotImplementedError

    def env(self, req: ExecRequest) -> Dict[str, str]:
        env = {"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        env.update(req.env)
        return env

    def check(self, req: ExecRequest) -> Optional[str]:
        """Refuse requests this launcher cannot run safely (e.g. as_root without a root path)."""
        return None

    async def signal(self, proc: asyncio.subprocess.Process, sig: int) -> None:
        raise NotImplementedError


class DirectLauncher(Launcher):
    """Runs as the agent's own account in a new process group. For development and tests."""

    def check(self, req: ExecRequest) -> Optional[str]:
        if req.as_root:
            return "as_root is not available with the direct launcher"
        return None

    def argv(self, req: ExecRequest) -> List[str]:
        if req.argv is not None:
            return list(req.argv)
        return ["/bin/sh", "-c", req.command or ""]

    async def signal(self, proc: asyncio.subprocess.Process, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)  # start_new_session made the child a group leader
        except ProcessLookupError:
            pass
        except OSError as exc:
            log.warning("cannot signal process group %s: %s", proc.pid, exc)


class Executor:
    def __init__(
        self,
        launcher: Launcher,
        max_concurrent: int = 2,
        max_output_bytes: int = 1024 * 1024,
        flush_interval: float = 0.2,
        flush_bytes: int = 8192,
        kill_grace: float = 5.0,
    ) -> None:
        self.launcher = launcher
        self.max_concurrent = max_concurrent
        self.max_output_bytes = max_output_bytes
        self.flush_interval = flush_interval
        self.flush_bytes = flush_bytes
        self.kill_grace = kill_grace
        self._procs: Dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: Dict[str, bool] = {}

    def running(self) -> List[str]:
        return list(self._procs)

    async def cancel(self, run_id: str) -> bool:
        proc = self._procs.get(run_id)
        if proc is None:
            return False
        self._cancelled[run_id] = True
        await self._terminate(proc)
        return True

    async def cancel_all(self) -> None:
        await asyncio.gather(*(self.cancel(r) for r in list(self._procs)))

    async def run(self, req: ExecRequest, sink: OutputSink) -> ExecResult:
        started = time.monotonic()
        problem = req.validate() or self.launcher.check(req)
        if problem:
            return ExecResult(req.run_id, "rejected", reason=problem)
        if req.run_id in self._procs:
            return ExecResult(req.run_id, "rejected", reason="duplicate run_id")
        if len(self._procs) >= self.max_concurrent:
            return ExecResult(req.run_id, "rejected", reason="busy")

        try:
            proc = await asyncio.create_subprocess_exec(
                *self.launcher.argv(req),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.launcher.env(req),
                cwd=req.cwd,
                start_new_session=True,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            return ExecResult(
                req.run_id, "failed_to_start", reason=str(exc), duration_ms=_ms(started)
            )

        self._procs[req.run_id] = proc
        out = _OutputPump(sink, self.max_output_bytes, self.flush_interval, self.flush_bytes)
        tasks = [
            asyncio.ensure_future(out.pump("stdout", proc.stdout)),
            asyncio.ensure_future(out.pump("stderr", proc.stderr)),
        ]
        ticker = asyncio.ensure_future(out.tick())
        status = "ok"
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=req.timeout)
            except asyncio.TimeoutError:
                status = "timeout"
                await self._terminate(proc)
            # Grandchildren that kept the pipes open must not hang the result forever.
            await asyncio.wait(tasks, timeout=self.kill_grace)
        finally:
            for task in tasks + [ticker]:
                task.cancel()
            self._procs.pop(req.run_id, None)
            cancelled = self._cancelled.pop(req.run_id, False)
            if proc.returncode is None:  # run() itself was cancelled (agent shutdown)
                await self._terminate(proc)
        await out.flush()

        if cancelled:
            status = "cancelled"
        elif status == "ok" and proc.returncode != 0:
            status = "error"
        return ExecResult(
            req.run_id,
            status,
            exit_code=proc.returncode,
            duration_ms=_ms(started),
            output_bytes=out.total,
            truncated=out.truncated,
        )

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the whole run, then SIGKILL whatever is left after the grace period."""
        if proc.returncode is not None:
            return
        await self.launcher.signal(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self.kill_grace)
        except asyncio.TimeoutError:
            await self.launcher.signal(proc, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.kill_grace)
            except asyncio.TimeoutError:
                log.error("process %s survived SIGKILL", proc.pid)


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

    async def pump(self, stream: str, reader: Optional[asyncio.StreamReader]) -> None:
        if reader is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            data = await reader.read(4096)
            if not data:
                break
            await self._add(stream, decoder.decode(data), len(data))
        tail = decoder.decode(b"", final=True)
        if tail:
            await self._add(stream, tail, 0)

    async def _add(self, stream: str, text: str, nbytes: int) -> None:
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
