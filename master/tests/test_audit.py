"""Audit chain: hashes link, tampering is detected, triggers refuse UPDATE/DELETE, secrets are
redacted before storage."""

from __future__ import annotations

import sqlite3

import pytest
from cluster_common.redact import Redactor

from cluster_master.audit import GENESIS, can_append_without_update, record_sync, verify_sync
from cluster_master.db import SyncDB


@pytest.fixture
def db(tmp_path) -> SyncDB:
    d = SyncDB(str(tmp_path / "audit.db"))
    d.migrate()
    yield d
    d.close()


def _record(db: SyncDB, n: int = 3) -> list[dict]:
    return [
        record_sync(
            db,
            actor_type="user",
            actor_id="alice",
            channel="web",
            action=f"test.{i}",
            detail={"i": i},
            ts_ms=1_700_000_000_000 + i,
        )
        for i in range(n)
    ]


def test_chain_links_and_verifies(db):
    rows = _record(db, 5)
    assert rows[0]["prev_hash"] == GENESIS
    for prev, row in zip(rows, rows[1:], strict=False):
        assert row["prev_hash"] == prev["hash"]
    result = verify_sync(db)
    assert result.ok and result.rows == 5
    assert result.head_id == 5 and result.head_hash == rows[-1]["hash"]


def test_hashes_are_deterministic(tmp_path):
    a = SyncDB(str(tmp_path / "a.db"))
    b = SyncDB(str(tmp_path / "b.db"))
    a.migrate()
    b.migrate()
    ra = _record(a, 2)
    rb = _record(b, 2)
    assert [r["hash"] for r in ra] == [r["hash"] for r in rb]
    a.close()
    b.close()


def test_update_and_delete_are_refused(db):
    _record(db, 2)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("UPDATE audit_log SET action='x' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.conn.execute("DELETE FROM audit_log WHERE id=1")
    assert can_append_without_update(db.conn)


def test_tamper_is_detected_even_without_triggers(db):
    _record(db, 3)
    db.conn.execute("DROP TRIGGER audit_no_update")
    db.conn.execute("UPDATE audit_log SET detail='{\"i\":99}' WHERE id=2")
    result = verify_sync(db)
    assert not result.ok and result.first_bad_id == 2
    assert "modified" in result.reason


def test_deleted_row_is_detected(db):
    _record(db, 3)
    db.conn.execute("DROP TRIGGER audit_no_delete")
    db.conn.execute("DELETE FROM audit_log WHERE id=2")
    result = verify_sync(db)
    assert not result.ok and result.first_bad_id == 3
    assert "gap" in result.reason


def test_replaced_tail_is_detected(db):
    _record(db, 2)
    db.conn.execute("DROP TRIGGER audit_no_delete")
    db.conn.execute("DELETE FROM audit_log WHERE id=2")
    # a forged row 2 with a wrong prev_hash
    db.conn.execute(
        "INSERT INTO audit_log (id, ts_ms, actor_type, actor_id, channel, action, detail,"
        " prev_hash, hash) VALUES (2, 1, 'user', 'mallory', 'web', 'x', '{}', ?, ?)",
        ("f" * 64, "0" * 64),
    )
    result = verify_sync(db)
    assert not result.ok and result.first_bad_id == 2
    assert "prev_hash" in result.reason


def test_detail_is_redacted(db):
    redactor = Redactor()
    redactor.register("hunter2hunter2", "password")
    row = record_sync(
        db,
        actor_type="service",
        actor_id="telegram-bot",
        channel="telegram",
        action="x",
        detail={"token": "cat_" + "a" * 43, "pw": "hunter2hunter2", "ok": "fine"},
        redactor=redactor,
    )
    assert "cat_" not in row["detail"]
    assert "hunter2" not in row["detail"]
    assert "[REDACTED:agent-token]" in row["detail"]
    assert "[REDACTED:password]" in row["detail"]
    assert "fine" in row["detail"]
    assert verify_sync(db).ok


def test_bad_actor_or_channel_rejected(db):
    with pytest.raises(ValueError):
        record_sync(db, actor_type="alien", actor_id="x", channel="web", action="a")
    with pytest.raises(ValueError):
        record_sync(db, actor_type="user", actor_id="x", channel="carrier-pigeon", action="a")
    assert verify_sync(db).rows == 0


def test_float_detail_is_refused(db):
    # canonical JSON has no floats: a detail with one must be rejected, not silently rounded
    with pytest.raises(Exception):  # noqa: B017 - CanonicalError is a ValueError subclass
        record_sync(
            db, actor_type="user", actor_id="x", channel="web", action="a", detail={"f": 1.5}
        )
    assert verify_sync(db).rows == 0
