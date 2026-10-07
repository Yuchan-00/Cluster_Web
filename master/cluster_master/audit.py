"""Hash-chained, append-only audit log (docs/design/security.md 13.1).

hash = SHA-256(prev_hash || canonical_json(row without hash)). Rows are written inside a
BEGIN IMMEDIATE transaction that reads the previous hash, so two writers (the master and
the admin CLI) cannot interleave. Detail is redacted before it is stored.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from cluster_common.canonical_json import VERSION as CANONICAL_VERSION
from cluster_common.canonical_json import canonical_bytes, sha256_hex
from cluster_common.redact import Redactor, default_redactor

from .db import Database, SyncDB, now_ms

GENESIS = "0" * 64
ACTOR_TYPES = ("user", "service", "ai", "node", "system", "cli")
CHANNELS = ("web", "telegram", "ai", "cli", "agent", "system")

_COLUMNS = (
    "id",
    "ts_ms",
    "actor_type",
    "actor_id",
    "on_behalf_of",
    "channel",
    "action",
    "target",
    "detail",
    "ip",
    "approval_id",
    "prev_hash",
)


def row_hash(row: dict[str, Any]) -> str:
    body = {k: row[k] for k in _COLUMNS}
    return sha256_hex(row["prev_hash"].encode("ascii") + canonical_bytes(body))


@dataclass
class VerifyResult:
    ok: bool
    rows: int
    first_bad_id: int | None = None
    reason: str | None = None
    head_id: int | None = None
    head_hash: str | None = None


def record_sync(
    db: SyncDB,
    *,
    actor_type: str,
    actor_id: str,
    channel: str,
    action: str,
    detail: dict[str, Any] | None = None,
    target: str | None = None,
    on_behalf_of: str | None = None,
    ip: str | None = None,
    approval_id: int | None = None,
    redactor: Redactor = default_redactor,
    ts_ms: int | None = None,
) -> dict[str, Any]:
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"bad actor_type {actor_type!r}")
    if channel not in CHANNELS:
        raise ValueError(f"bad channel {channel!r}")
    detail = redactor.redact_obj(detail or {})
    detail_text = canonical_bytes(detail).decode("utf-8")
    with db.transaction(immediate=True):
        last = db.fetchone("SELECT id, hash FROM audit_log ORDER BY id DESC LIMIT 1")
        row = {
            "id": (last["id"] + 1) if last else 1,
            "ts_ms": ts_ms if ts_ms is not None else now_ms(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "on_behalf_of": on_behalf_of,
            "channel": channel,
            "action": action,
            "target": target,
            "detail": detail_text,
            "ip": ip,
            "approval_id": approval_id,
            "prev_hash": last["hash"] if last else GENESIS,
        }
        row["hash"] = row_hash(row)
        db.execute(
            "INSERT INTO audit_log (id, ts_ms, actor_type, actor_id, on_behalf_of, channel, action,"
            " target, detail, ip, approval_id, prev_hash, hash) VALUES (:id, :ts_ms, :actor_type,"
            " :actor_id, :on_behalf_of, :channel, :action, :target, :detail, :ip, :approval_id,"
            " :prev_hash, :hash)",
            row,
        )
    return row


def verify_sync(db: SyncDB) -> VerifyResult:
    prev, count, head_id, head_hash = GENESIS, 0, None, None
    expected_id = 1
    for r in db.conn.execute("SELECT * FROM audit_log ORDER BY id"):
        row = dict(r)
        if row["id"] != expected_id:
            return VerifyResult(False, count, row["id"], f"gap before id {row['id']}")
        if row["prev_hash"] != prev:
            return VerifyResult(False, count, row["id"], "prev_hash does not match previous row")
        if row_hash(row) != row["hash"]:
            return VerifyResult(False, count, row["id"], "row hash mismatch (modified row)")
        prev, head_id, head_hash = row["hash"], row["id"], row["hash"]
        count += 1
        expected_id += 1
    return VerifyResult(True, count, head_id=head_id, head_hash=head_hash)


def head_sync(db: SyncDB) -> tuple[int | None, str | None]:
    last = db.fetchone("SELECT id, hash FROM audit_log ORDER BY id DESC LIMIT 1")
    return (last["id"], last["hash"]) if last else (None, None)


class AuditLog:
    """Async wrapper used by the master process."""

    def __init__(self, db: Database, redactor: Redactor = default_redactor) -> None:
        self.db = db
        self.redactor = redactor

    async def record(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("redactor", self.redactor)
        return await self.db.run(lambda db: record_sync(db, **kwargs))

    async def verify(self) -> VerifyResult:
        return await self.db.run(verify_sync)

    async def head(self) -> tuple[int | None, str | None]:
        return await self.db.run(head_sync)

    async def tail(self, limit: int = 100, before_id: int | None = None) -> list[dict[str, Any]]:
        def _tail(db: SyncDB) -> list[dict[str, Any]]:
            if before_id is None:
                rows = db.fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
            else:
                rows = db.fetchall(
                    "SELECT * FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT ?",
                    (before_id, limit),
                )
            return [dict(r) for r in rows]

        return await self.db.run(_tail)


def can_append_without_update(conn: sqlite3.Connection) -> bool:
    """True if the append-only triggers are present (checked at startup)."""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    return {"audit_no_update", "audit_no_delete"} <= names


__all__ = [
    "GENESIS",
    "CANONICAL_VERSION",
    "AuditLog",
    "VerifyResult",
    "record_sync",
    "verify_sync",
    "head_sync",
    "row_hash",
    "can_append_without_update",
]
