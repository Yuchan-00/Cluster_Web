"""Lockdown kill switch (docs/design/security.md 16).

State lives in `system_state` so a restart comes back locked. Switching publishes
`system.lockdown`; the agent hub subscribes and broadcasts `lockdown`/`unlock` to every agent,
and new agents learn the state from `welcome`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..audit import AuditLog
from ..db import Database, SyncDB, row_dict
from ..events import EventBus

log = logging.getLogger(__name__)

KEY = "lockdown"


class LockdownService:
    def __init__(self, db: Database, audit: AuditLog, bus: EventBus) -> None:
        self.db = db
        self.audit = audit
        self.bus = bus
        self.active = False
        self.reason: str | None = None
        self.changed_ms: int | None = None
        self.changed_by: str | None = None

    async def load(self) -> bool:
        row = await self.db.run(
            lambda db: row_dict(db.fetchone("SELECT * FROM system_state WHERE key=?", (KEY,)))
        )
        if row:
            value = row["value"]
            self.active = value.startswith("on")
            self.reason = (value[3:] or None) if self.active else None
            self.changed_ms = row["changed_ms"]
            self.changed_by = row["changed_by"]
        if self.active:
            log.warning("starting in LOCKDOWN (set by %s): %s", self.changed_by, self.reason)
        return self.active

    async def set(
        self,
        active: bool,
        *,
        actor: tuple[str, str, str],
        reason: str | None = None,
        ip: str | None = None,
    ) -> bool:
        """Switch lockdown; returns True if the state changed."""
        actor_type, actor_id, channel = actor
        reason = (reason or "").strip()[:200] or None
        if active == self.active:
            return False
        now_ms = int(time.time() * 1000)
        value = ("on:" + (reason or "")) if active else "off"

        def _write(db: SyncDB) -> None:
            with db.transaction(immediate=True):
                db.execute(
                    "INSERT INTO system_state (key, value, changed_ms, changed_by)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                    " changed_ms=excluded.changed_ms, changed_by=excluded.changed_by",
                    (KEY, value, now_ms, actor_id),
                )

        await self.db.run(_write)
        self.active = active
        self.reason = reason if active else None
        self.changed_ms = now_ms
        self.changed_by = actor_id
        await self.audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            channel=channel,
            action="system.lockdown" if active else "system.unlock",
            detail={"reason": reason},
            ip=ip,
        )
        if active:
            log.critical("LOCKDOWN enabled by %s: %s", actor_id, reason)
        else:
            log.warning("lockdown lifted by %s", actor_id)
        await self.bus.publish(
            "system.lockdown", active=active, reason=reason, by=actor_id, ts_ms=now_ms
        )
        return True

    def view(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "changed_ms": self.changed_ms,
            "changed_by": self.changed_by,
        }


__all__ = ["LockdownService"]
