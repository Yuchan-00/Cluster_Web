"""Agent WebSocket hub: /ws/agent (docs/PLAN.md 12.1, security.md 8).

One connection per node. The token travels in the upgrade request (`Authorization: Bearer
cat_...` + `X-Node-Id`); a bad token gets an HTTP 401 before the handshake completes. After
`hello` the connection is pinned to the node id it authenticated as. Everything the agent
sends is validated (`models.py`), counted against the per-connection limits and, for command
traffic, checked against the runs this master issued to that node.

Close codes: 4401 auth, 4403 identity mismatch, 4408 silent (no metrics), 4409 duplicate,
4429 rate limit, 1008 protocol violation, 1009 too big, 1001 master shutting down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..audit import AuditLog
from ..config import AgentLimits
from ..events import EventBus
from ..models import (
    NODE_ID,
    CmdOutput,
    CmdResult,
    ExecSpec,
    Hello,
    Metrics,
    ProtocolError,
    json_size,
    parse_agent_message,
    parse_hello,
    welcome,
)
from ..secrets import NODE_PREFIX, is_token
from ..services.alerts import AlertService
from ..services.lockdown import LockdownService
from ..services.metrics_store import MetricsStore, filtered_extra
from ..services.nodes import NodeNotFound, NodeRegistry

log = logging.getLogger(__name__)

CLOSE_AUTH = 4401
CLOSE_IDENTITY = 4403
CLOSE_SILENT = 4408
CLOSE_DUPLICATE = 4409
CLOSE_RATE = 4429
CLOSE_PROTOCOL = 1008
CLOSE_TOO_BIG = 1009
CLOSE_GOING_AWAY = 1001

MAX_VIOLATIONS = 10  # protocol slips tolerated per connection before 1008
AUTH_FAIL_WINDOW_S = 600.0
AUTH_FAIL_ALERT = 5
OUTPUT_QUEUE_CHUNKS = 1024  # per run, before chunks are dropped (Phase 4 spools to disk)
SEND_TIMEOUT_S = 10.0
RUN_GRACE_S = 120.0  # after the spec's timeout, a run without a result is reported lost
WATCHDOG_INTERVAL_S = 1.0


class HubError(RuntimeError):
    pass


class NodeOffline(HubError):
    pass


class LockdownActive(HubError):
    pass


class TokenBucket:
    def __init__(self, rate: float, burst: float, clock=time.monotonic) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.clock = clock
        self.updated = clock()

    def take(self, n: float = 1.0) -> bool:
        now = self.clock()
        self.tokens = min(self.burst, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


@dataclass
class RunHandle:
    """A command issued to a node. Output arrives on `output` (None marks the end); `result`
    resolves with the agent's cmd_result, or a synthetic `lost` result."""

    run_id: str
    node_id: str
    spec: ExecSpec
    issued: float = field(default_factory=time.monotonic)
    output: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(OUTPUT_QUEUE_CHUNKS))
    result: asyncio.Future = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    output_bytes: int = 0
    dropped_chunks: int = 0

    def finish(self, result: CmdResult) -> None:
        if not self.result.done():
            self.result.set_result(result)
        self._end_output()

    def _end_output(self) -> None:
        try:
            self.output.put_nowait(None)
        except asyncio.QueueFull:
            # The consumer is behind by a full queue; drop the newest chunk for the sentinel.
            try:
                self.output.get_nowait()
                self.dropped_chunks += 1
            except asyncio.QueueEmpty:
                pass
            self.output.put_nowait(None)


@dataclass
class Connection:
    node_id: str
    ws: WebSocket
    peer: str | None
    hello: Hello
    limits: AgentLimits
    clock: Any = time.monotonic
    connected: float = 0.0
    last_message: float = 0.0
    last_metrics: float | None = None
    violations: int = 0
    msg_bucket: TokenBucket = field(init=False)
    byte_bucket: TokenBucket = field(init=False)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False
    close_code: int | None = None
    close_reason: str | None = None
    disconnect_code: int | None = None

    def __post_init__(self) -> None:
        now = self.clock()
        self.connected = self.last_message = now
        self.msg_bucket = TokenBucket(self.limits.msgs_per_s, self.limits.msgs_per_s, self.clock)
        self.byte_bucket = TokenBucket(
            self.limits.bytes_per_s, float(self.limits.bytes_per_s), self.clock
        )

    def view(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "peer": self.peer,
            "agent_version": self.hello.agent_version,
            "connected_s": round(self.clock() - self.connected, 1),
            "since_last_message_s": round(self.clock() - self.last_message, 1),
            "violations": self.violations,
        }


def new_run_id() -> str:
    return secrets.token_urlsafe(16)  # 22 chars, [A-Za-z0-9_-]


def encode(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


class AgentHub:
    def __init__(
        self,
        limits: AgentLimits,
        nodes: NodeRegistry,
        metrics: MetricsStore,
        alerts: AlertService,
        lockdown: LockdownService,
        bus: EventBus,
        audit: AuditLog,
        *,
        clock=time.monotonic,
    ) -> None:
        self.limits = limits
        self.nodes = nodes
        self.metrics = metrics
        self.alerts = alerts
        self.lockdown = lockdown
        self.bus = bus
        self.audit = audit
        self.clock = clock
        self._conns: dict[str, Connection] = {}
        self._runs: dict[str, RunHandle] = {}
        self._auth_failures: dict[str, deque[float]] = {}
        self._watchdog: asyncio.Task | None = None
        self._unsubscribe = None

    # -- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        unsub_lockdown = self.bus.subscribe(self._on_lockdown_event, "system.lockdown")
        unsub_nodes = self.bus.subscribe(self._on_node_event, "node.updated", "node.removed")

        def _unsubscribe() -> None:
            unsub_lockdown()
            unsub_nodes()

        self._unsubscribe = _unsubscribe
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="agent-hub-watchdog")

    async def stop(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for conn in list(self._conns.values()):
            await self._close(conn, CLOSE_GOING_AWAY, "master shutting down")
        for handle in list(self._runs.values()):
            handle.finish(_lost(handle, "master shutting down"))
        self._runs.clear()

    def connections(self) -> list[dict[str, Any]]:
        return [c.view() for c in self._conns.values()]

    def is_online(self, node_id: str) -> bool:
        return node_id in self._conns

    # -- endpoint --------------------------------------------------------------------------

    async def handle(self, ws: WebSocket) -> None:
        peer = ws.client[0] if ws.client else None
        node_id = ws.headers.get("x-node-id", "")
        scheme, _, token = ws.headers.get("authorization", "").partition(" ")
        token = token.strip()
        claimed = node_id if NODE_ID.match(node_id) else None
        if (
            claimed is None
            or scheme.lower() != "bearer"
            or not is_token(token, NODE_PREFIX)
            or not await self.nodes.authenticate(claimed, token)
        ):
            await self._auth_failed(ws, peer, claimed)
            return

        await ws.accept()
        try:
            raw = await asyncio.wait_for(self._receive(ws), self.limits.hello_timeout_s)
        except asyncio.TimeoutError:
            await _close_ws(ws, CLOSE_PROTOCOL, "hello timeout")
            return
        except WebSocketDisconnect:
            return
        if raw is None:
            await _close_ws(ws, CLOSE_PROTOCOL, "binary frame")
            return
        try:
            hello = parse_hello(raw)
        except ProtocolError as exc:
            log.warning("node %s from %s: %s", claimed, peer, exc)
            await _close_ws(ws, exc.close_code, str(exc)[:120])
            return
        if hello.node_id != claimed:
            await self._identity_mismatch(ws, peer, claimed, hello.node_id)
            return
        if json_size(hello.static_info) > self.limits.static_info_max_bytes:
            await _close_ws(ws, CLOSE_TOO_BIG, "static_info too large")
            return

        conn = Connection(claimed, ws, peer, hello, self.limits, self.clock)
        if not await self._admit(conn):
            return
        try:
            await self._session(conn)
        finally:
            await self._drop(conn, conn_reason(conn))

    async def _admit(self, conn: Connection) -> bool:
        existing = self._conns.get(conn.node_id)
        if existing is not None:
            stale = self.clock() - existing.last_message > 2 * self.limits.metrics_interval_s
            if stale:
                log.warning(
                    "node %s: replacing silent connection from %s with %s",
                    conn.node_id,
                    existing.peer,
                    conn.peer,
                )
                await self._close(existing, CLOSE_DUPLICATE, "replaced by a new connection")
                await self._drop(existing, "replaced")
            else:
                await self._duplicate(conn, existing)
                return False
        self._conns[conn.node_id] = conn
        try:
            await self.nodes.on_connect(
                conn.node_id,
                peer=conn.peer,
                agent_version=conn.hello.agent_version,
                board=conn.hello.board,
                static_info=conn.hello.static_info,
                running_commands=list(conn.hello.running_commands),
            )
        except NodeNotFound:
            # Removed between authenticate() and now.
            self._conns.pop(conn.node_id, None)
            await _close_ws(conn.ws, CLOSE_AUTH, "node removed")
            return False
        status = self.nodes.status(conn.node_id)
        if any("board" in w for w in status.warnings):
            await self.alerts.raise_(
                "node_board_mismatch", node_id=conn.node_id, message="; ".join(status.warnings)
            )
        await self.alerts.resolve("node_offline", node_id=conn.node_id)
        unknown = [r for r in conn.hello.running_commands if r not in self._runs]
        if unknown:
            log.warning(
                "node %s reports %d running command(s) this master did not issue: %s",
                conn.node_id,
                len(unknown),
                unknown[:5],
            )
        try:
            await self._send(
                conn,
                welcome(
                    metrics_interval=self.limits.metrics_interval_s,
                    lockdown=self.lockdown.active,
                    lease_ttl_s=self.limits.lease_ttl_s,
                    work_request_interval_s=self.limits.work_request_interval_s,
                    server_time=time.time(),
                ),
            )
        except HubError:
            await self._drop(conn, "welcome failed")
            return False
        log.info(
            "node %s online from %s (agent %s, %d pending result(s))",
            conn.node_id,
            conn.peer,
            conn.hello.agent_version,
            len(conn.hello.pending_results),
        )
        return True

    async def _session(self, conn: Connection) -> None:
        ws = conn.ws
        while True:
            try:
                raw = await self._receive(ws)
            except WebSocketDisconnect as exc:
                conn.closing = True
                conn.disconnect_code = exc.code
                return
            if raw is None:
                await self._close(conn, CLOSE_PROTOCOL, "binary frame")
                return
            now = self.clock()
            conn.last_message = now
            size = len(raw.encode("utf-8"))
            if not conn.msg_bucket.take() or not conn.byte_bucket.take(size):
                await self._rate_limited(conn, size)
                return
            try:
                msg = parse_agent_message(raw)
            except ProtocolError as exc:
                if not await self._violation(conn, str(exc), exc.close_code):
                    return
                continue
            try:
                ok = await self._dispatch(conn, msg, now)
            except Exception:  # noqa: BLE001 - one bad message must not take the node down
                log.exception("node %s: error handling %s", conn.node_id, msg.type)
                ok = True
            if not ok:
                return

    async def _dispatch(self, conn: Connection, msg: Any, now: float) -> bool:
        if isinstance(msg, Metrics):
            return await self._on_metrics(conn, msg, now)
        if isinstance(msg, CmdOutput):
            return await self._on_output(conn, msg)
        if isinstance(msg, CmdResult):
            return await self._on_result(conn, msg)
        return True  # pong

    async def _on_metrics(self, conn: Connection, m: Metrics, now: float) -> bool:
        if (
            conn.last_metrics is not None
            and now - conn.last_metrics < self.limits.metrics_min_interval_s
        ):
            return await self._violation(conn, "metrics faster than the minimum interval")
        conn.last_metrics = now
        extra = filtered_extra(m.extra())
        if (
            len(extra) > self.limits.extra_max_keys
            or json_size(extra) > self.limits.extra_max_bytes
        ):
            m.data["extra"] = {}
            if not await self._violation(conn, "metrics.extra too large"):
                return False
        else:
            m.data["extra"] = extra
        sample = self.metrics.add(conn.node_id, m)
        self.nodes.set_sched(conn.node_id, m.sched.model_dump() if m.sched is not None else None)
        await self.nodes.on_message(conn.node_id, conn.peer)
        await self.bus.publish(
            "metrics",
            node_id=conn.node_id,
            ts=float(m.ts),
            sample=sample.as_dict(),
            data=m.data,
            sched=m.sched.model_dump() if m.sched is not None else None,
        )
        return True

    async def _on_output(self, conn: Connection, msg: CmdOutput) -> bool:
        handle = self._runs.get(msg.run_id)
        if handle is None or handle.node_id != conn.node_id:
            return await self._violation(conn, "cmd_output for a run not issued to this node")
        size = len(msg.data.encode("utf-8"))
        if size > self.limits.output_chunk_max_bytes:
            return await self._violation(conn, "cmd_output chunk too large")
        handle.output_bytes += size
        try:
            handle.output.put_nowait((msg.stream, msg.data))
        except asyncio.QueueFull:
            handle.dropped_chunks += 1
        await self.bus.publish(
            "cmd_output", run_id=msg.run_id, node_id=conn.node_id, stream=msg.stream, data=msg.data
        )
        return True

    async def _on_result(self, conn: Connection, msg: CmdResult) -> bool:
        handle = self._runs.get(msg.run_id)
        if handle is None or handle.node_id != conn.node_id:
            return await self._violation(conn, "cmd_result for a run not issued to this node")
        del self._runs[msg.run_id]
        handle.finish(msg)
        status = self.nodes.status(conn.node_id)
        if msg.run_id in status.running_commands:
            status.running_commands.remove(msg.run_id)
        await self.bus.publish(
            "cmd_result", run_id=msg.run_id, node_id=conn.node_id, result=msg.model_dump()
        )
        await self.bus.publish(
            "command.finished", run_id=msg.run_id, node_id=conn.node_id, status=msg.status
        )
        return True

    # -- commands (used by Phase 4's command service and by tests) ------------------------

    async def exec(self, node_id: str, spec: ExecSpec) -> RunHandle:
        if self.lockdown.active:
            raise LockdownActive("lockdown is active")
        conn = self._conns.get(node_id)
        if conn is None:
            raise NodeOffline(f"node {node_id} is offline")
        if spec.run_id in self._runs:
            raise HubError(f"run {spec.run_id} already issued")
        handle = RunHandle(spec.run_id, node_id, spec)
        self._runs[spec.run_id] = handle
        try:
            await self._send(conn, spec.to_message())
        except HubError:
            self._runs.pop(spec.run_id, None)
            raise
        self.nodes.status(node_id).running_commands.append(spec.run_id)
        return handle

    async def cancel(self, node_id: str, run_id: str) -> bool:
        handle = self._runs.get(run_id)
        if handle is None or handle.node_id != node_id:
            return False
        conn = self._conns.get(node_id)
        if conn is None:
            return False
        await self._send(conn, {"type": "cancel", "run_id": run_id})
        return True

    def run(self, run_id: str) -> RunHandle | None:
        return self._runs.get(run_id)

    async def set_metrics_interval(self, node_id: str, interval: float) -> None:
        conn = self._conns.get(node_id)
        if conn is None:
            raise NodeOffline(f"node {node_id} is offline")
        await self._send(conn, {"type": "config", "metrics_interval": interval})

    # -- lockdown --------------------------------------------------------------------------

    async def _on_lockdown_event(self, event: Any) -> None:
        active = bool(event.data.get("active"))
        msg = {"type": "lockdown" if active else "unlock"}
        for conn in list(self._conns.values()):
            try:
                await self._send(conn, msg)
            except HubError:
                pass
        if active:
            for handle in list(self._runs.values()):
                # the agents cancel everything themselves; report it to the issuers now
                handle.finish(_synthetic(handle, "cancelled", "lockdown"))
            self._runs.clear()

    async def _on_node_event(self, event: Any) -> None:
        """A revoked token or a removed node disconnects the live agent at once."""
        node_id = event.data.get("node_id")
        conn = self._conns.get(node_id)
        if conn is None:
            return
        if event.type == "node.removed":
            await self._close(conn, CLOSE_AUTH, "node removed")
            await self._drop(conn, "removed")
        elif {"token_revoked", "token_rotated"} & set(event.data.get("changes", ())):
            await self._close(conn, CLOSE_AUTH, "token changed")
            await self._drop(conn, "token changed")

    # -- limits, violations, security events -----------------------------------------------

    async def _violation(
        self, conn: Connection, what: str, close_code: int = CLOSE_PROTOCOL
    ) -> bool:
        conn.violations += 1
        log.warning("node %s: %s (%d/%d)", conn.node_id, what, conn.violations, MAX_VIOLATIONS)
        if conn.violations >= MAX_VIOLATIONS or close_code != CLOSE_PROTOCOL:
            await self._close(conn, close_code, what[:120])
            return False
        return True

    async def _rate_limited(self, conn: Connection, size: int) -> None:
        log.warning("node %s: rate limit exceeded (last message %d bytes)", conn.node_id, size)
        await self.alerts.raise_(
            "agent_rate_limit",
            node_id=conn.node_id,
            message=f"agent from {conn.peer} exceeded the message rate limit; disconnected",
        )
        await self._close(conn, CLOSE_RATE, "rate limit")

    async def _auth_failed(self, ws: WebSocket, peer: str | None, claimed: str | None) -> None:
        key = peer or "uds"
        now = self.clock()
        window = self._auth_failures.setdefault(key, deque())
        while window and now - window[0] > AUTH_FAIL_WINDOW_S:
            window.popleft()
        window.append(now)
        log.warning(
            "agent auth failed from %s (claimed node %r), %d recent", peer, claimed, len(window)
        )
        if len(window) == 1:
            await self.audit.record(
                actor_type="node",
                actor_id=claimed or "?",
                channel="agent",
                action="agent.auth_failed",
                target=claimed,
                detail={"peer": peer},
                ip=peer,
            )
        if len(window) == AUTH_FAIL_ALERT:
            await self.alerts.raise_(
                "agent_auth_failures",
                node_id=claimed if claimed and self.nodes.get(claimed) else None,
                message=f"{len(window)} failed agent logins from {peer} in 10 minutes"
                + (f" (claiming {claimed})" if claimed else ""),
            )
        try:
            await ws.send_denial_response(
                Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
            )
        except RuntimeError:
            await _close_ws(ws, CLOSE_AUTH, "authentication failed")

    async def _identity_mismatch(
        self, ws: WebSocket, peer: str | None, claimed: str, hello_id: str
    ) -> None:
        log.error("node %s from %s sent hello as %s", claimed, peer, hello_id)
        await self.audit.record(
            actor_type="node",
            actor_id=claimed,
            channel="agent",
            action="agent.identity_mismatch",
            target=claimed,
            detail={"hello_node_id": hello_id, "peer": peer},
            ip=peer,
        )
        await self.alerts.raise_(
            "agent_identity",
            node_id=claimed,
            message=f"token of {claimed} used by an agent calling itself {hello_id} ({peer})",
        )
        await _close_ws(ws, CLOSE_IDENTITY, "identity mismatch")

    async def _duplicate(self, new: Connection, existing: Connection) -> None:
        log.error(
            "node %s: duplicate connection from %s while %s is live",
            new.node_id,
            new.peer,
            existing.peer,
        )
        await self.audit.record(
            actor_type="node",
            actor_id=new.node_id,
            channel="agent",
            action="agent.duplicate",
            target=new.node_id,
            detail={"peer": new.peer, "existing_peer": existing.peer},
            ip=new.peer,
        )
        await self.alerts.raise_(
            "agent_duplicate",
            node_id=new.node_id,
            message=f"second agent for {new.node_id} from {new.peer} "
            f"while {existing.peer} is connected (stolen token?)",
        )
        await _close_ws(new.ws, CLOSE_DUPLICATE, "already connected")

    # -- plumbing --------------------------------------------------------------------------

    async def _receive(self, ws: WebSocket) -> str | None:
        """Next text frame, or None for a binary frame. Raises WebSocketDisconnect."""
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(message.get("code", 1005), message.get("reason"))
        text = message.get("text")
        if text is None:
            return None
        return text

    async def _send(self, conn: Connection, msg: dict[str, Any]) -> None:
        data = encode(msg)
        try:
            async with conn.send_lock:
                await asyncio.wait_for(conn.ws.send_text(data), SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self._close(conn, CLOSE_PROTOCOL, "send timeout")
            raise HubError(f"node {conn.node_id}: send timed out") from None
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            conn.closing = True
            raise HubError(f"node {conn.node_id}: send failed: {exc}") from None

    async def _close(self, conn: Connection, code: int, reason: str) -> None:
        if conn.closing:
            return
        conn.closing = True
        conn.close_code = code
        conn.close_reason = reason
        await _close_ws(conn.ws, code, reason)

    async def _drop(self, conn: Connection, reason: str) -> None:
        if self._conns.get(conn.node_id) is conn:
            del self._conns[conn.node_id]
            log.info("node %s offline (%s)", conn.node_id, reason)
            try:
                await self.nodes.on_disconnect(conn.node_id, reason)
            except Exception:  # noqa: BLE001
                log.exception("node %s: on_disconnect failed", conn.node_id)
            if self.nodes.get(conn.node_id) is not None:
                await self.alerts.raise_(
                    "node_offline",
                    node_id=conn.node_id,
                    message=f"{conn.node_id} offline: {reason}",
                )

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            now = self.clock()
            for conn in list(self._conns.values()):
                if now - conn.last_message > self.limits.offline_after_s and not conn.closing:
                    await self._close(conn, CLOSE_SILENT, "no messages")
                    await self._drop(conn, "no messages")
            for run_id, handle in list(self._runs.items()):
                if now - handle.issued > handle.spec.timeout + RUN_GRACE_S:
                    del self._runs[run_id]
                    handle.finish(_lost(handle, "no result from the agent"))
                    await self.bus.publish(
                        "command.finished", run_id=run_id, node_id=handle.node_id, status="lost"
                    )


def conn_reason(conn: Connection) -> str:
    if conn.close_reason:
        return conn.close_reason
    if conn.disconnect_code is not None:
        return f"agent closed ({conn.disconnect_code})"
    return "connection closed"


async def _close_ws(ws: WebSocket, code: int, reason: str) -> None:
    try:
        if ws.client_state != WebSocketState.DISCONNECTED:
            await asyncio.wait_for(ws.close(code=code, reason=reason[:120]), SEND_TIMEOUT_S)
    except (RuntimeError, OSError, asyncio.TimeoutError, WebSocketDisconnect):
        pass


def _synthetic(handle: RunHandle, status: str, reason: str) -> CmdResult:
    return CmdResult(
        type="cmd_result",
        run_id=handle.run_id,
        status=status,  # type: ignore[arg-type]
        reason=reason,
        duration_ms=int((time.monotonic() - handle.issued) * 1000),
        output_bytes=handle.output_bytes,
    )


def _lost(handle: RunHandle, reason: str) -> CmdResult:
    return _synthetic(handle, "failed_to_start" if handle.output_bytes == 0 else "error", reason)


__all__ = [
    "CLOSE_AUTH",
    "CLOSE_DUPLICATE",
    "CLOSE_IDENTITY",
    "CLOSE_RATE",
    "CLOSE_SILENT",
    "AgentHub",
    "HubError",
    "LockdownActive",
    "NodeOffline",
    "RunHandle",
    "TokenBucket",
    "new_run_id",
]
