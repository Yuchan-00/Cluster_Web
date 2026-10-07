"""Node registry (docs/PLAN.md 13 `nodes`, security.md 8.2-8.3).

The database row is the admin's view of a node (name, board, labels, capacity, token hash);
`NodeStatus` is what the agent hub observes at runtime. Labels and capacity are set here and
never taken from the agent: a compromised node must not be able to promote itself to the BPU
pool or claim more memory than it has.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..audit import AuditLog
from ..db import Database, SyncDB, now_ms, row_dict
from ..events import EventBus
from ..models import BOARDS, NODE_ID
from ..secrets import NODE_PREFIX, hash_token, is_token, new_token, token_matches

log = logging.getLogger(__name__)

SCHED_STATES = ("active", "cordoned", "draining", "drained")
LABEL_KEY = r"^[a-z][a-z0-9_-]{0,31}$"
MAX_LABELS = 32
CAPACITY_KEYS = {"slots": (0, 64), "bpu_slots": (0, 64), "job_mem_mb": (0, 1 << 20)}
# Hash compared when the node is unknown or revoked so that the response time does not say
# which of the two it was (security.md 8.2).
_DUMMY_HASH = hash_token("cat_" + "A" * 43)
SEEN_WRITE_INTERVAL_S = 60.0  # last_seen goes to the SD card at most this often per node


class NodeError(ValueError):
    pass


class NodeNotFound(NodeError):
    pass


class NodeExists(NodeError):
    pass


@dataclass
class NodeStatus:
    """Runtime state kept by the agent hub; never persisted except last_seen/last_ip."""

    online: bool = False
    connected_at: float | None = None
    last_seen: float | None = None  # wall clock of the last message from the agent
    peer: str | None = None
    agent_version: str | None = None
    reported_board: str | None = None
    static_info: dict[str, Any] = field(default_factory=dict)
    running_commands: list[str] = field(default_factory=list)
    sched: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    disconnect_reason: str | None = None


@dataclass
class NodeRecord:
    id: str
    board: str
    labels: dict[str, Any]
    capacity: dict[str, int]
    sched_state: str
    sched_reason: str | None
    has_token: bool
    registered_ip: str | None
    last_seen_ms: int | None
    last_ip: str | None
    created_ms: int
    created_by: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> NodeRecord:
        return cls(
            id=row["id"],
            board=row["board"],
            labels=json.loads(row["labels"] or "{}"),
            capacity=json.loads(row["capacity"] or "{}"),
            sched_state=row["sched_state"],
            sched_reason=row["sched_reason"],
            has_token=row["token_hash"] is not None,
            registered_ip=row["registered_ip"],
            last_seen_ms=row["last_seen_ms"],
            last_ip=row["last_ip"],
            created_ms=row["created_ms"],
            created_by=row["created_by"],
        )


def validate_node_id(node_id: Any) -> str:
    if not isinstance(node_id, str) or not NODE_ID.match(node_id):
        raise NodeError("node id must match ^[a-z0-9][a-z0-9-]{0,62}$")
    return node_id


def validate_labels(labels: Any) -> dict[str, Any]:
    if labels is None:
        return {}
    if not isinstance(labels, dict) or len(labels) > MAX_LABELS:
        raise NodeError(f"labels must be an object with at most {MAX_LABELS} keys")
    import re

    out: dict[str, Any] = {}
    for key, value in labels.items():
        if not isinstance(key, str) or not re.match(LABEL_KEY, key):
            raise NodeError(f"bad label key {key!r}")
        if isinstance(value, bool) or (isinstance(value, str) and len(value) <= 64):
            out[key] = value
        elif isinstance(value, int) and -(1 << 31) <= value < (1 << 31):
            out[key] = value
        else:
            raise NodeError(f"label {key!r} must be a short string, an int or a bool")
    return out


def validate_capacity(capacity: Any) -> dict[str, int]:
    if capacity is None:
        return {}
    if not isinstance(capacity, dict):
        raise NodeError("capacity must be an object")
    out: dict[str, int] = {}
    for key, value in capacity.items():
        if key not in CAPACITY_KEYS:
            raise NodeError(f"unknown capacity key {key!r}")
        lo, hi = CAPACITY_KEYS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise NodeError(f"capacity.{key} must be an integer between {lo} and {hi}")
        out[key] = value
    return out


def validate_board(board: Any) -> str:
    if board not in BOARDS:
        raise NodeError(f"board must be one of {', '.join(BOARDS)}")
    return board


class NodeRegistry:
    def __init__(self, db: Database, audit: AuditLog, bus: EventBus) -> None:
        self.db = db
        self.audit = audit
        self.bus = bus
        self._records: dict[str, NodeRecord] = {}
        self._status: dict[str, NodeStatus] = {}
        self._seen_written: dict[str, float] = {}

    # -- persistence -----------------------------------------------------------------------

    async def load(self) -> None:
        rows = await self.db.run(lambda db: [dict(r) for r in db.fetchall("SELECT * FROM nodes")])
        self._records = {r["id"]: NodeRecord.from_row(r) for r in rows}
        for node_id in self._records:
            self._status.setdefault(node_id, NodeStatus())
        log.info("loaded %d node(s)", len(self._records))

    async def register(
        self,
        node_id: str,
        board: str,
        *,
        labels: dict[str, Any] | None = None,
        capacity: dict[str, int] | None = None,
        actor: tuple[str, str, str],
        ip: str | None = None,
    ) -> tuple[NodeRecord, str]:
        """Create a node and return its token. The token is shown exactly once."""
        node_id = validate_node_id(node_id)
        board = validate_board(board)
        labels = validate_labels(labels)
        capacity = validate_capacity(capacity)
        token = new_token(NODE_PREFIX)
        actor_type, actor_id, channel = actor

        def _insert(db: SyncDB) -> dict[str, Any]:
            with db.transaction(immediate=True):
                if db.fetchone("SELECT 1 FROM nodes WHERE id=?", (node_id,)):
                    raise NodeExists(f"node {node_id} already exists")
                db.execute(
                    "INSERT INTO nodes (id, board, token_hash, labels, capacity, created_ms,"
                    " created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        node_id,
                        board,
                        hash_token(token),
                        json.dumps(labels, sort_keys=True),
                        json.dumps(capacity, sort_keys=True),
                        now_ms(),
                        actor_id,
                    ),
                )
                return dict(db.fetchone("SELECT * FROM nodes WHERE id=?", (node_id,)))

        row = await self.db.run(_insert)
        record = NodeRecord.from_row(row)
        self._records[node_id] = record
        self._status.setdefault(node_id, NodeStatus())
        await self.audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            channel=channel,
            action="node.register",
            target=node_id,
            detail={"board": board, "labels": labels, "capacity": capacity},
            ip=ip,
        )
        await self.bus.publish("node.registered", node_id=node_id, board=board)
        return record, token

    async def update(
        self,
        node_id: str,
        *,
        labels: dict[str, Any] | None = None,
        capacity: dict[str, int] | None = None,
        sched_state: str | None = None,
        sched_reason: str | None = None,
        actor: tuple[str, str, str],
        ip: str | None = None,
    ) -> NodeRecord:
        record = self.require(node_id)
        changes: dict[str, Any] = {}
        if labels is not None:
            changes["labels"] = json.dumps(validate_labels(labels), sort_keys=True)
        if capacity is not None:
            changes["capacity"] = json.dumps(validate_capacity(capacity), sort_keys=True)
        if sched_state is not None:
            if sched_state not in SCHED_STATES:
                raise NodeError(f"sched_state must be one of {', '.join(SCHED_STATES)}")
            changes["sched_state"] = sched_state
            changes["sched_reason"] = sched_reason
        if not changes:
            return record
        sets = ", ".join(f"{k}=:{k}" for k in changes)
        params = dict(changes, id=node_id)

        def _update(db: SyncDB) -> dict[str, Any]:
            with db.transaction(immediate=True):
                db.execute(f"UPDATE nodes SET {sets} WHERE id=:id", params)  # noqa: S608 - keys are ours
                return dict(db.fetchone("SELECT * FROM nodes WHERE id=?", (node_id,)))

        row = await self.db.run(_update)
        self._records[node_id] = NodeRecord.from_row(row)
        actor_type, actor_id, channel = actor
        await self.audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            channel=channel,
            action="node.update",
            target=node_id,
            detail={
                k: (json.loads(v) if k in ("labels", "capacity") else v) for k, v in changes.items()
            },
            ip=ip,
        )
        await self.bus.publish("node.updated", node_id=node_id, changes=sorted(changes))
        return self._records[node_id]

    async def rotate_token(
        self, node_id: str, *, actor: tuple[str, str, str], ip: str | None = None
    ) -> str:
        self.require(node_id)
        token = new_token(NODE_PREFIX)
        await self._set_token_hash(node_id, hash_token(token))
        actor_type, actor_id, channel = actor
        await self.audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            channel=channel,
            action="node.token_rotate",
            target=node_id,
            ip=ip,
        )
        await self.bus.publish("node.updated", node_id=node_id, changes=["token_rotated"])
        return token

    async def revoke_token(
        self, node_id: str, *, actor: tuple[str, str, str], ip: str | None = None
    ) -> None:
        self.require(node_id)
        await self._set_token_hash(node_id, None)
        actor_type, actor_id, channel = actor
        await self.audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            channel=channel,
            action="node.token_revoke",
            target=node_id,
            ip=ip,
        )
        await self.bus.publish("node.updated", node_id=node_id, changes=["token_revoked"])

    async def _set_token_hash(self, node_id: str, token_hash: str | None) -> None:
        def _set(db: SyncDB) -> None:
            with db.transaction(immediate=True):
                db.execute("UPDATE nodes SET token_hash=? WHERE id=?", (token_hash, node_id))

        await self.db.run(_set)
        self._records[node_id].has_token = token_hash is not None

    async def remove(
        self, node_id: str, *, actor: tuple[str, str, str], ip: str | None = None
    ) -> None:
        self.require(node_id)

        def _delete(db: SyncDB) -> None:
            with db.transaction(immediate=True):
                db.execute("DELETE FROM nodes WHERE id=?", (node_id,))

        await self.db.run(_delete)
        self._records.pop(node_id, None)
        self._status.pop(node_id, None)
        self._seen_written.pop(node_id, None)
        actor_type, actor_id, channel = actor
        await self.audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            channel=channel,
            action="node.remove",
            target=node_id,
            ip=ip,
        )
        await self.bus.publish("node.removed", node_id=node_id)

    # -- authentication (called by the agent hub) --------------------------------------------

    async def authenticate(self, node_id: str, token: str) -> bool:
        """Constant-time check of a presented token against the stored hash.

        The hash is read from the database on every attempt so that a revocation from the CLI
        takes effect immediately, even if the master's cache is stale.
        """
        if not isinstance(node_id, str) or not NODE_ID.match(node_id):
            token_matches(token if is_token(token, NODE_PREFIX) else "cat_" + "B" * 43, _DUMMY_HASH)
            return False
        row = await self.db.run(
            lambda db: row_dict(db.fetchone("SELECT token_hash FROM nodes WHERE id=?", (node_id,)))
        )
        stored = row["token_hash"] if row and row["token_hash"] else None
        ok = token_matches(token, stored or _DUMMY_HASH)
        return ok and stored is not None

    # -- runtime status ----------------------------------------------------------------------

    def get(self, node_id: str) -> NodeRecord | None:
        return self._records.get(node_id)

    def require(self, node_id: str) -> NodeRecord:
        record = self._records.get(node_id)
        if record is None:
            raise NodeNotFound(f"unknown node {node_id}")
        return record

    def status(self, node_id: str) -> NodeStatus:
        return self._status.setdefault(node_id, NodeStatus())

    def ids(self) -> list[str]:
        return sorted(self._records)

    def online_ids(self) -> list[str]:
        return sorted(n for n, s in self._status.items() if s.online and n in self._records)

    async def on_connect(
        self,
        node_id: str,
        *,
        peer: str | None,
        agent_version: str,
        board: str,
        static_info: dict[str, Any],
        running_commands: list[str],
    ) -> NodeStatus:
        record = self.require(node_id)
        status = self.status(node_id)
        now = time.time()
        status.online = True
        status.connected_at = now
        status.last_seen = now
        status.peer = peer
        status.agent_version = agent_version
        status.reported_board = board
        status.static_info = static_info
        status.running_commands = list(running_commands)
        status.disconnect_reason = None
        status.warnings = self._warnings(record, board, static_info)
        for warning in status.warnings:
            log.warning("node %s: %s", node_id, warning)
        await self._write_seen(node_id, peer, now, force=True)
        await self.bus.publish("node.online", node_id=node_id, peer=peer, warnings=status.warnings)
        return status

    async def on_disconnect(self, node_id: str, reason: str) -> None:
        status = self.status(node_id)
        if not status.online:
            return
        status.online = False
        status.disconnect_reason = reason
        status.sched = None
        now = time.time()
        if node_id in self._records:
            await self._write_seen(node_id, status.peer, status.last_seen or now, force=True)
        await self.bus.publish("node.offline", node_id=node_id, reason=reason)

    async def on_message(self, node_id: str, peer: str | None) -> None:
        status = self.status(node_id)
        now = time.time()
        status.last_seen = now
        await self._write_seen(node_id, peer, now, force=False)

    def set_sched(self, node_id: str, sched: dict[str, Any] | None) -> None:
        self.status(node_id).sched = sched

    async def _write_seen(self, node_id: str, ip: str | None, when: float, *, force: bool) -> None:
        last = self._seen_written.get(node_id, 0.0)
        if not force and when - last < SEEN_WRITE_INTERVAL_S:
            return
        self._seen_written[node_id] = when
        ms = int(when * 1000)

        def _write(db: SyncDB) -> None:
            db.execute(
                "UPDATE nodes SET last_seen_ms=?, last_ip=?,"
                " registered_ip=COALESCE(registered_ip, ?) WHERE id=?",
                (ms, ip, ip, node_id),
            )

        await self.db.run(_write)
        record = self._records.get(node_id)
        if record is not None:
            record.last_seen_ms = ms
            record.last_ip = ip
            if record.registered_ip is None:
                record.registered_ip = ip

    @staticmethod
    def _warnings(record: NodeRecord, board: str, static_info: dict[str, Any]) -> list[str]:
        warnings = []
        if board != record.board:
            warnings.append(f"agent reports board {board!r}, registered as {record.board!r}")
        for key in ("labels", "capacity"):
            reported = static_info.get(key)
            if isinstance(reported, dict):
                mine = getattr(record, key)
                if reported != mine and (mine or reported):
                    warnings.append(f"agent-reported {key} ignored (differs from registry)")
        return warnings

    # -- views -----------------------------------------------------------------------------

    def view(self, node_id: str) -> dict[str, Any]:
        record = self.require(node_id)
        status = self.status(node_id)
        return {
            "id": record.id,
            "board": record.board,
            "labels": record.labels,
            "capacity": record.capacity,
            "sched_state": record.sched_state,
            "sched_reason": record.sched_reason,
            "has_token": record.has_token,
            "created_ms": record.created_ms,
            "created_by": record.created_by,
            "last_seen_ms": record.last_seen_ms,
            "last_ip": record.last_ip,
            "online": status.online,
            "connected_at": status.connected_at,
            "agent_version": status.agent_version,
            "reported_board": status.reported_board,
            "static_info": status.static_info,
            "running_commands": status.running_commands,
            "sched": status.sched,
            "warnings": status.warnings,
            "disconnect_reason": status.disconnect_reason,
        }

    def views(self) -> list[dict[str, Any]]:
        return [self.view(n) for n in self.ids()]


__all__ = [
    "CAPACITY_KEYS",
    "SCHED_STATES",
    "NodeError",
    "NodeExists",
    "NodeNotFound",
    "NodeRecord",
    "NodeRegistry",
    "NodeStatus",
    "validate_board",
    "validate_capacity",
    "validate_labels",
    "validate_node_id",
]
