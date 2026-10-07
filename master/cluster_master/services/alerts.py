"""Alerts (docs/PLAN.md 13 `alerts`, Phase 5 adds thresholds and Telegram delivery).

An alert is identified by (kind, node_id); raising it twice is a no-op while it is open, and
resolving it closes the open row. Phase 2 raises alerts for node offline and for the agent
channel's security events (duplicate connection, identity mismatch, rate-limit kicks).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db import Database, SyncDB, row_dict
from ..events import EventBus

log = logging.getLogger(__name__)

LEVELS = ("warning", "critical")
KINDS = {
    "node_offline": "warning",
    "node_board_mismatch": "warning",
    "agent_duplicate": "critical",
    "agent_identity": "critical",
    "agent_rate_limit": "warning",
    "agent_auth_failures": "critical",
    "master_restart": "warning",
}


class AlertService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    async def raise_(
        self,
        kind: str,
        *,
        node_id: str | None,
        message: str,
        level: str | None = None,
    ) -> dict[str, Any] | None:
        """Open an alert; returns the new row, or None if it was already open."""
        level = level or KINDS.get(kind, "warning")
        if level not in LEVELS:
            raise ValueError(f"bad alert level {level!r}")
        if len(message) > 1000:
            message = message[:997] + "..."
        now_ms = int(time.time() * 1000)

        def _insert(db: SyncDB) -> dict[str, Any] | None:
            with db.transaction(immediate=True):
                existing = db.fetchone(
                    "SELECT id FROM alerts WHERE kind=? AND node_id IS ? AND resolved_ms IS NULL",
                    (kind, node_id),
                )
                if existing:
                    return None
                cur = db.execute(
                    "INSERT INTO alerts (node_id, kind, level, message, started_ms)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (node_id, kind, level, message, now_ms),
                )
                return row_dict(db.fetchone("SELECT * FROM alerts WHERE id=?", (cur.lastrowid,)))

        row = await self.db.run(_insert)
        if row is not None:
            log.warning("alert %s [%s] %s: %s", kind, level, node_id or "-", message)
            await self.bus.publish("alert.raised", alert=row)
        return row

    async def resolve(self, kind: str, *, node_id: str | None) -> dict[str, Any] | None:
        now_ms = int(time.time() * 1000)

        def _resolve(db: SyncDB) -> dict[str, Any] | None:
            with db.transaction(immediate=True):
                row = db.fetchone(
                    "SELECT id FROM alerts WHERE kind=? AND node_id IS ? AND resolved_ms IS NULL",
                    (kind, node_id),
                )
                if not row:
                    return None
                db.execute("UPDATE alerts SET resolved_ms=? WHERE id=?", (now_ms, row["id"]))
                return row_dict(db.fetchone("SELECT * FROM alerts WHERE id=?", (row["id"],)))

        row = await self.db.run(_resolve)
        if row is not None:
            await self.bus.publish("alert.resolved", alert=row)
        return row

    async def resolve_node(self, node_id: str) -> int:
        """Close every open alert of a removed node."""
        now_ms = int(time.time() * 1000)

        def _resolve(db: SyncDB) -> int:
            with db.transaction(immediate=True):
                cur = db.execute(
                    "UPDATE alerts SET resolved_ms=? WHERE node_id=? AND resolved_ms IS NULL",
                    (now_ms, node_id),
                )
                return cur.rowcount

        return await self.db.run(_resolve)

    async def list(
        self, *, open_only: bool = False, limit: int = 100, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))

        def _list(db: SyncDB) -> list[dict[str, Any]]:
            where = ["resolved_ms IS NULL"] if open_only else []
            params: list[Any] = []
            if before_id is not None:
                where.append("id < ?")
                params.append(before_id)
            sql = "SELECT * FROM alerts"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            return [dict(r) for r in db.fetchall(sql, tuple(params))]

        return await self.db.run(_list)

    async def open_count(self) -> int:
        row = await self.db.run(
            lambda db: db.fetchone("SELECT COUNT(*) AS n FROM alerts WHERE resolved_ms IS NULL")
        )
        return int(row["n"]) if row else 0


__all__ = ["KINDS", "LEVELS", "AlertService"]
