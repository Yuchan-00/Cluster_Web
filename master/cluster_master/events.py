"""In-process event bus (docs/PLAN.md 4.4).

Producers publish; consumers (UI hub, alert service, notifier, audit) subscribe. A failing
consumer is logged and skipped so that, for example, a Telegram outage never blocks the hub.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

EVENT_TYPES = {
    "node.online",
    "node.offline",
    "node.registered",
    "node.removed",
    "node.updated",
    "metrics",
    "alert.raised",
    "alert.resolved",
    "command.finished",
    "cmd_output",
    "cmd_result",
    "job.finished",
    "ai_task.finished",
    "approval.requested",
    "approval.decided",
    "security.login",
    "security.telegram_link",
    "system.lockdown",
}

Handler = Callable[["Event"], Awaitable[None] | None]


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str | None, list[Handler]] = {}

    def subscribe(self, handler: Handler, *types: str) -> Callable[[], None]:
        """Subscribe to the given types (none = every event). Returns an unsubscribe function."""
        keys: list[str | None] = list(types) or [None]
        for key in keys:
            if key is not None and key not in EVENT_TYPES:
                raise ValueError(f"unknown event type {key!r}")
            self._handlers.setdefault(key, []).append(handler)

        def unsubscribe() -> None:
            for key in keys:
                handlers = self._handlers.get(key, [])
                if handler in handlers:
                    handlers.remove(handler)

        return unsubscribe

    async def publish(self, type_: str, **data: Any) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_!r}")
        event = Event(type_, data)
        for handler in list(self._handlers.get(type_, [])) + list(self._handlers.get(None, [])):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - one consumer must not break the others
                log.exception("event handler %r failed on %s", handler, type_)
        return event

    def publish_soon(self, type_: str, **data: Any) -> None:
        """Fire-and-forget from synchronous code."""
        asyncio.ensure_future(self.publish(type_, **data))
