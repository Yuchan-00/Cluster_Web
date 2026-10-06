"""The agent's session logic: hello/welcome, metrics, exec/cancel, lockdown, reconnects.

Messages follow docs/PLAN.md 12.1. Commands keep running across reconnects (the master link
and the execd link are independent); their results are queued and sent after the next hello,
which lists them in `pending_results` so the master does not declare those runs lost.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import time
from typing import Any, Callable, Deque, Dict, List, Optional

from websockets.exceptions import ConnectionClosed

from . import __version__
from .collectors import MetricsCollector
from .config import CommandLimits
from .connection import (
    CLOSE_AUTH,
    CLOSE_DUPLICATE,
    CLOSE_IDENTITY,
    CLOSE_RATE,
    Backoff,
    close_code,
    connect,
    handshake_status,
)
from .executor import ExecRequest, ExecResult, Executor

log = logging.getLogger(__name__)

WELCOME_TIMEOUT = 10.0
AUTH_RETRY_S = 300.0  # a revoked token will not fix itself; do not hammer the master
DUPLICATE_RETRY_S = 60.0
RATE_RETRY_S = 60.0
STABLE_SESSION_S = 30.0  # only a session that lasted this long resets the reconnect backoff
MAX_PENDING_RESULTS = 200
OUTPUT_BYTES_PER_S = 256 * 1024  # serialized bytes; well under the master's 1 MiB/s
OUTPUT_MSGS_PER_S = 30  # master allows 50 messages/s per connection in total
OUTPUT_PIECE_CHARS = 5000  # <= 30 KB once JSON-escaped (6 bytes/char worst case) < 64 KiB cap
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # same rule as cluster-execd
EXTRA_KEYS = ("bpu", "throttled", "core_volts", "reboot_required", "isolation_mode")


class TokenBucket:
    def __init__(self, rate: float, burst: float, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.clock = clock
        self.updated = clock()

    def take(self, amount: float) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


def encode(msg: Dict[str, Any]) -> str:
    """Compact JSON; falls back to ASCII escapes for strings that are not valid UTF-8
    (a lone surrogate would otherwise fail inside websockets and wedge the result queue)."""
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False)
    try:
        data.encode("utf-8")
    except UnicodeEncodeError:
        data = json.dumps(msg, separators=(",", ":"), ensure_ascii=True)
    return data


class Agent:
    def __init__(
        self,
        node_id: str,
        master_url: str,
        token: str,
        collector: MetricsCollector,
        executor: Executor,
        static_extra: Optional[Dict[str, Any]] = None,
        ssl_ctx: Any = None,
        metrics_interval: float = 5.0,
        command_limits: Optional[CommandLimits] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.node_id = node_id
        self.master_url = master_url
        self.token = token
        self.collector = collector
        self.executor = executor
        self.static_extra = static_extra or {}
        self.ssl_ctx = ssl_ctx
        self.metrics_interval = metrics_interval
        self.limits = command_limits or CommandLimits()
        self.lockdown = False
        self.connected = asyncio.Event()
        self.pending_results: Deque[Dict[str, Any]] = collections.deque(maxlen=MAX_PENDING_RESULTS)
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()
        self._runs: Dict[str, asyncio.Task] = {}
        self._dropped: Dict[str, int] = {}
        self._clock = clock
        self._out_bytes = TokenBucket(OUTPUT_BYTES_PER_S, 2 * OUTPUT_BYTES_PER_S, clock)
        self._out_msgs = TokenBucket(OUTPUT_MSGS_PER_S, 2 * OUTPUT_MSGS_PER_S, clock)
        self._stopping = asyncio.Event()

    # -- lifecycle ------------------------------------------------------------------------

    async def run_forever(self) -> None:
        backoff = Backoff()
        while not self._stopping.is_set():
            delay = backoff.next()
            started: Optional[float] = None
            try:
                async with connect(self.master_url, self.token, self.node_id, self.ssl_ctx) as ws:
                    if self._stopping.is_set():  # stop() arrived during the handshake
                        break
                    started = self._clock()
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - every failure means "reconnect later"
                delay = self._retry_delay(exc, delay)
            finally:
                self._ws = None
                self.connected.clear()
            if started is not None and self._clock() - started >= STABLE_SESSION_S:
                backoff.reset()  # a master that drops us right away keeps escalating
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _retry_delay(self, exc: BaseException, default: float) -> float:
        status, code = handshake_status(exc), close_code(exc)
        if status == 401 or code in (CLOSE_AUTH, CLOSE_IDENTITY):
            log.error(
                "master rejected this node's token (status=%s close=%s); "
                "re-register the node or reinstall the token",
                status,
                code,
            )
            return AUTH_RETRY_S
        if code == CLOSE_DUPLICATE:
            log.error(
                "master reports a duplicate connection for %s: is another agent "
                "running with this token?",
                self.node_id,
            )
            return DUPLICATE_RETRY_S
        if code == CLOSE_RATE:
            log.error("master closed the connection for exceeding its rate limit")
            return RATE_RETRY_S
        log.warning(
            "connection to master failed: %s: %s (retry in %.0fs)", type(exc).__name__, exc, default
        )
        return default

    async def stop(self) -> None:
        self._stopping.set()
        await self.executor.cancel_all()
        if self._ws is not None:
            await self._ws.close()

    # -- session --------------------------------------------------------------------------

    def hello(self) -> Dict[str, Any]:
        static = self.collector.static_info()
        static.update(self.static_extra)
        return {
            "type": "hello",
            "node_id": self.node_id,
            "board": self.collector.board,
            "agent_version": __version__,
            "static_info": static,
            "running_tasks": [],
            "unacked_results": [],
            "orphaned": [],
            "running_commands": sorted(set(self.executor.running()) | set(self._runs)),
            # results that finished while disconnected; they follow right after welcome
            "pending_results": [m["run_id"] for m in self.pending_results],
        }

    async def _session(self, ws: Any) -> None:
        self._ws = ws
        await self._send(self.hello())
        welcome = await asyncio.wait_for(self._recv(ws), WELCOME_TIMEOUT)
        if welcome.get("type") != "welcome":
            raise ConnectionError(f"expected welcome, got {welcome.get('type')!r}")
        self._apply_welcome(welcome)
        self.connected.set()
        log.info("connected to master as %s (lockdown=%s)", self.node_id, self.lockdown)
        await self._flush_results()
        tasks = {
            asyncio.ensure_future(self._receive_loop(ws)),
            asyncio.ensure_future(self._metrics_loop()),
        }
        try:
            # Either loop ending (closed socket, failed send) ends the session: a silent
            # metrics loop would look like a dead node to the master.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()

    async def _receive_loop(self, ws: Any) -> None:
        while True:
            msg = await self._recv(ws)
            try:
                await self._dispatch(msg)
            except Exception:  # noqa: BLE001 - one bad message must not drop the session
                log.exception("error handling %r message", msg.get("type"))

    async def _recv(self, ws: Any) -> Dict[str, Any]:
        raw = await ws.recv()
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ConnectionError("master sent invalid JSON") from exc
        if not isinstance(msg, dict):
            raise ConnectionError("master sent a non-object message")
        return msg

    def _apply_welcome(self, msg: Dict[str, Any]) -> None:
        self._apply_config(msg)
        if msg.get("lockdown") is True:
            self._enter_lockdown()
        elif msg.get("lockdown") is False:
            self.lockdown = False

    def _apply_config(self, msg: Dict[str, Any]) -> None:
        interval = msg.get("metrics_interval")
        if isinstance(interval, (int, float)) and not isinstance(interval, bool):
            if interval == interval:  # not NaN
                self.metrics_interval = min(60.0, max(1.0, float(interval)))

    async def _send(self, msg: Dict[str, Any]) -> int:
        """Send one message and return its serialized size."""
        ws = self._ws
        if ws is None:
            raise ConnectionError("not connected")
        data = encode(msg)
        async with self._send_lock:
            await ws.send(data)
        return len(data.encode("utf-8"))

    # -- messages from the master ---------------------------------------------------------

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "exec":
            self._start_run(msg)
        elif kind == "cancel":
            run_id = msg.get("run_id")
            if isinstance(run_id, str):  # termination can take seconds; keep receiving
                asyncio.ensure_future(self.executor.cancel(run_id))
        elif kind == "lockdown":
            self._enter_lockdown()
        elif kind == "unlock":
            self.lockdown = False
            log.warning("lockdown lifted by master")
        elif kind == "config":
            self._apply_config(msg)
        else:
            log.debug("ignoring message type %r", kind)  # newer master, older agent

    def _enter_lockdown(self) -> None:
        if not self.lockdown:
            log.warning("lockdown: refusing all exec requests until the master unlocks")
        self.lockdown = True
        # cancel_all also covers runs that are still starting (security.md 16)
        asyncio.ensure_future(self.executor.cancel_all())

    def _start_run(self, msg: Dict[str, Any]) -> None:
        run_id = msg.get("run_id")
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            # Not echoed back: an id we cannot validate cannot be put into a result either.
            log.warning("exec with an invalid run_id ignored")
            return
        if run_id in self._runs or run_id in self.executor.running():
            log.warning("duplicate exec for running %s ignored", run_id)
            return
        if self.lockdown:
            self._queue_result(ExecResult(run_id, "rejected", reason="lockdown").to_message())
            return
        try:
            req = ExecRequest.from_message(msg, default_timeout=self.limits.default_timeout)
        except (ValueError, TypeError, OverflowError) as exc:
            self._queue_result(ExecResult(run_id, "rejected", reason=str(exc)).to_message())
            return
        if req.timeout > self.limits.max_timeout:
            reason = f"timeout exceeds this node's limit of {self.limits.max_timeout:.0f}s"
            self._queue_result(ExecResult(run_id, "rejected", reason=reason).to_message())
            return
        task = asyncio.ensure_future(self._run(req))
        self._runs[run_id] = task
        task.add_done_callback(lambda _t, r=run_id: self._runs.pop(r, None))

    async def _run(self, req: ExecRequest) -> None:
        if self.lockdown:  # lockdown arrived between scheduling and starting
            self._queue_result(ExecResult(req.run_id, "rejected", reason="lockdown").to_message())
            return
        self._dropped[req.run_id] = 0

        async def sink(stream: str, text: str) -> None:
            await self._send_output(req.run_id, stream, text)

        try:
            result = await self.executor.run(req, sink)
        except Exception as exc:  # noqa: BLE001 - always report something for a run_id
            log.exception("run %s failed", req.run_id)
            result = ExecResult(req.run_id, "failed_to_start", reason=f"agent error: {exc}")
        result.dropped_bytes = self._dropped.pop(req.run_id, 0)
        self._queue_result(result.to_message())

    async def _send_output(self, run_id: str, stream: str, text: str) -> None:
        for piece in _pieces(text):
            msg = {"type": "cmd_output", "run_id": run_id, "stream": stream, "data": piece}
            size = len(encode(msg).encode("utf-8"))
            # Output is the only droppable traffic: metrics double as heartbeat and must get
            # through. The budget is charged with the bytes that actually go on the wire.
            if not self.connected.is_set() or not (
                self._out_msgs.take(1) and self._out_bytes.take(size)
            ):
                self._dropped[run_id] = self._dropped.get(run_id, 0) + len(piece.encode("utf-8"))
                continue
            try:
                await self._send(msg)
            except Exception:  # noqa: BLE001 - connection lost: the result is queued later
                self._dropped[run_id] = self._dropped.get(run_id, 0) + len(piece.encode("utf-8"))

    def _queue_result(self, msg: Dict[str, Any]) -> None:
        self.pending_results.append(msg)
        if self.connected.is_set():
            asyncio.ensure_future(self._flush_results())

    async def _flush_results(self) -> None:
        async with self._flush_lock:  # one flusher at a time, or a result could go out twice
            while self.pending_results and self.connected.is_set():
                try:
                    await self._send(self.pending_results[0])
                except (ConnectionError, OSError, ConnectionClosed):
                    return  # stays queued for the next session
                except Exception as exc:  # noqa: BLE001 - never let one item wedge the queue
                    log.error("dropping unsendable result %r: %s", self.pending_results[0], exc)
                self.pending_results.popleft()

    # -- metrics --------------------------------------------------------------------------

    def metrics_message(self, sample: Dict[str, Any]) -> Dict[str, Any]:
        sample = dict(sample)
        ts = sample.pop("ts", time.time())
        extra = sample.get("extra") or {}
        sample["extra"] = {k: v for k, v in extra.items() if k in EXTRA_KEYS}
        capacity = self.static_extra.get("capacity", {})
        return {
            "type": "metrics",
            "ts": ts,
            "data": sample,
            "sched": {  # jobs arrive in Phase 7; until then everything is free
                "free_slots": capacity.get("slots", 0),
                "free_bpu_slots": capacity.get("bpu_slots", 0),
                "job_mem_free_mb": capacity.get("job_mem_mb", 0),
                "running": [],
                "cached_bundles": [],
            },
        }

    async def _metrics_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            # psutil and vcgencmd block; keep them off the event loop.
            sample = await loop.run_in_executor(None, self.collector.collect)
            await self._send(self.metrics_message(sample))
            await asyncio.sleep(self.metrics_interval)


def _pieces(text: str) -> List[str]:
    return [text[i : i + OUTPUT_PIECE_CHARS] for i in range(0, len(text), OUTPUT_PIECE_CHARS)]
