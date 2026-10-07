"""Browser WebSocket hub: /ws/ui (docs/PLAN.md 12.2).

Each browser connection receives the event stream as `{"type": "event", "event": <name>,
"ts": ..., "data": {...}}`. A client that cannot keep up is disconnected rather than allowed
to grow an unbounded queue. Browsers only send small control messages (`ping`, `subscribe`).

Authentication is the web listener's job (`require("viewer")` on the route); the hub adds the
Origin check that stops cross-site WebSocket hijacking.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from fastapi import WebSocket
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..auth.principal import Principal
from ..events import EVENT_TYPES, Event, EventBus

log = logging.getLogger(__name__)

QUEUE_SIZE = 256
MAX_CLIENT_MESSAGE = 4096
CLOSE_SLOW = 4000
CLOSE_ORIGIN = 4403
CLOSE_PROTOCOL = 1008
MAX_CLIENTS = 32
CLOSE_TIMEOUT_S = 5.0  # a browser with a full receive window must not stall shutdown
UI_EVENTS = frozenset(EVENT_TYPES - {"cmd_output"})  # raw command output goes via Phase 4's API


@dataclass(eq=False)  # identity semantics: clients live in a set
class UiClient:
    ws: WebSocket
    principal: Principal
    subscribed: frozenset[str] = UI_EVENTS
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(QUEUE_SIZE))
    connected: float = field(default_factory=time.time)
    sent: int = 0
    dropped: bool = False


class UiHub:
    def __init__(self, bus: EventBus, *, origins: list[str]) -> None:
        self.bus = bus
        self.origins = {o.rstrip("/").lower() for o in origins}
        self.hosts = {urlparse(o).netloc.lower() for o in self.origins}
        self._clients: set[UiClient] = set()
        self._unsubscribe = None

    async def start(self) -> None:
        self._unsubscribe = self.bus.subscribe(self._on_event)

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for client in list(self._clients):
            await _close(client.ws, 1001, "master shutting down")
        self._clients.clear()

    def count(self) -> int:
        return len(self._clients)

    # -- origin check ----------------------------------------------------------------------

    def origin_allowed(self, ws: WebSocket) -> bool:
        origin = ws.headers.get("origin")
        if origin is None:
            return True  # not a browser (curl, tests); authentication still applies
        origin = origin.rstrip("/").lower()
        if origin in self.origins:
            return True
        # Same-origin fallback for the loopback development master only: the Host must be a
        # loopback literal, so a DNS-rebound name pointing at 127.0.0.1 does not qualify.
        host = ws.headers.get("host", "").lower()
        parsed = urlparse(origin)
        if not host or parsed.netloc != host or parsed.scheme not in ("http", "https"):
            return False
        return host in self.hosts or _is_loopback_host(host)

    # -- endpoint --------------------------------------------------------------------------

    async def handle(self, ws: WebSocket, principal: Principal) -> None:
        if not self.origin_allowed(ws):
            log.warning(
                "ui: refused origin %r for %s", ws.headers.get("origin"), principal.describe()
            )
            await _deny(ws, 403, CLOSE_ORIGIN)
            return
        if len(self._clients) >= MAX_CLIENTS:
            await _deny(ws, 503, 1013)
            return
        await ws.accept()
        client = UiClient(ws, principal)
        self._clients.add(client)
        sender = asyncio.create_task(self._sender(client))
        try:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "ts": time.time(),
                        "user": principal.id,
                        "role": principal.role,
                    }
                )
            )
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None or len(text) > MAX_CLIENT_MESSAGE:
                    await _close(ws, CLOSE_PROTOCOL, "bad client message")
                    break
                if not self._on_client_message(client, text):
                    await _close(ws, CLOSE_PROTOCOL, "bad client message")
                    break
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(client)
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - the connection is gone either way
                log.debug("ui: sender task ended with an error", exc_info=True)

    def _on_client_message(self, client: UiClient, text: str) -> bool:
        try:
            msg = json.loads(text)
        except ValueError:
            return False
        if not isinstance(msg, dict):
            return False
        kind = msg.get("type")
        if kind == "ping":
            _offer(client, {"type": "pong", "ts": time.time()})
            return True
        if kind == "subscribe":
            events = msg.get("events")
            if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
                return False
            client.subscribed = frozenset(events) & UI_EVENTS
            return True
        return False

    async def _sender(self, client: UiClient) -> None:
        while True:
            item = await client.queue.get()
            if item is None:
                await _close(client.ws, CLOSE_SLOW, "client too slow")
                return
            try:
                await client.ws.send_text(item)
                client.sent += 1
            except (WebSocketDisconnect, RuntimeError, OSError):
                return

    # -- fan-out ---------------------------------------------------------------------------

    def _on_event(self, event: Event) -> None:
        if not self._clients or event.type not in UI_EVENTS:
            return
        payload = json.dumps(
            {"type": "event", "event": event.type, "ts": event.ts, "data": event.data},
            default=str,
            separators=(",", ":"),
        )
        for client in list(self._clients):
            if event.type in client.subscribed:
                _offer(client, payload)


def _offer(client: UiClient, payload: str | dict[str, Any]) -> None:
    if client.dropped:
        return
    text = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    try:
        client.queue.put_nowait(text)
    except asyncio.QueueFull:
        # A browser that is this far behind will not catch up; let it reconnect and resync.
        client.dropped = True
        log.warning("ui: dropping slow client %s", client.principal.describe())
        while True:
            try:
                client.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        client.queue.put_nowait(None)


def _is_loopback_host(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    elif name.startswith("[") and "]:" in host:
        name = host[1 : host.index("]")]
    return name in ("127.0.0.1", "localhost", "::1")


async def _deny(ws: WebSocket, status: int, close_code: int) -> None:
    """Refuse before the handshake completes: an HTTP status, or a close code as fallback."""
    try:
        await ws.send_denial_response(Response(status_code=status))
    except RuntimeError:
        await _close(ws, close_code, "refused")


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    try:
        if ws.client_state != WebSocketState.DISCONNECTED:
            await asyncio.wait_for(ws.close(code=code, reason=reason), CLOSE_TIMEOUT_S)
    except (RuntimeError, OSError, WebSocketDisconnect, asyncio.TimeoutError):
        pass


__all__ = ["UI_EVENTS", "UiHub"]
