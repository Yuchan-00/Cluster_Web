"""Per-node metrics: an in-memory ring buffer plus 1-minute rollups in SQLite.

The ring (default 720 samples = 1 h at 5 s) serves the dashboard; `metrics_1m` serves the
history charts and survives restarts. Rollups are written once a minute so the SD card sees one
small write per node per minute, not one per sample.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from ..db import Database, SyncDB
from ..models import EXTRA_KEYS, Metrics

log = logging.getLogger(__name__)

ROLLUP_INTERVAL_S = 60.0
RETENTION_DAYS = 30


@dataclass(frozen=True)
class Sample:
    ts: float  # agent wall clock
    received: float  # master wall clock
    cpu: float | None
    mem: float | None
    temp: float | None
    disk: float | None
    net_rx_bps: float | None
    net_tx_bps: float | None
    bpu: float | None  # average over BPU cores, if reported

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "cpu": self.cpu,
            "mem": self.mem,
            "temp": self.temp,
            "disk": self.disk,
            "net_rx_bps": self.net_rx_bps,
            "net_tx_bps": self.net_tx_bps,
            "bpu": self.bpu,
        }


def summarize(m: Metrics, received: float) -> Sample:
    net = m.data.get("net")
    rx = tx = None
    if isinstance(net, dict):
        rates = [v for v in net.values() if isinstance(v, dict)]
        rx_vals = [v.get("rx_bps") for v in rates if isinstance(v.get("rx_bps"), (int, float))]
        tx_vals = [v.get("tx_bps") for v in rates if isinstance(v.get("tx_bps"), (int, float))]
        rx = float(sum(rx_vals)) if rx_vals else None
        tx = float(sum(tx_vals)) if tx_vals else None
    bpu = m.extra().get("bpu")
    bpu_avg = None
    if isinstance(bpu, list):
        nums = [float(x) for x in bpu if isinstance(x, (int, float)) and not isinstance(x, bool)]
        bpu_avg = sum(nums) / len(nums) if nums else None
    return Sample(
        ts=float(m.ts),
        received=received,
        cpu=m.cpu_percent(),
        mem=m.mem_percent(),
        temp=m.temp_c(),
        disk=m.disk_percent(),
        net_rx_bps=rx,
        net_tx_bps=tx,
        bpu=bpu_avg,
    )


def filtered_extra(extra: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in extra.items() if k in EXTRA_KEYS}


class MetricsStore:
    def __init__(self, db: Database, ring_size: int = 720) -> None:
        self.db = db
        self.ring_size = ring_size
        self._rings: dict[str, deque[Sample]] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, list[Sample]] = {}  # samples since the last rollup
        self._last_rollup = time.time()

    # -- ingest ----------------------------------------------------------------------------

    def add(self, node_id: str, m: Metrics, received: float | None = None) -> Sample:
        received = received if received is not None else time.time()
        sample = summarize(m, received)
        ring = self._rings.get(node_id)
        if ring is None:
            ring = self._rings[node_id] = deque(maxlen=self.ring_size)
        ring.append(sample)
        self._pending.setdefault(node_id, []).append(sample)
        data = dict(m.data)
        data["extra"] = filtered_extra(m.extra())
        self._latest[node_id] = {"ts": float(m.ts), "received": received, "data": data}
        return sample

    def forget(self, node_id: str) -> None:
        self._rings.pop(node_id, None)
        self._latest.pop(node_id, None)
        self._pending.pop(node_id, None)

    # -- reads -----------------------------------------------------------------------------

    def latest(self, node_id: str) -> dict[str, Any] | None:
        return self._latest.get(node_id)

    def latest_sample(self, node_id: str) -> Sample | None:
        ring = self._rings.get(node_id)
        return ring[-1] if ring else None

    def recent(self, node_id: str, since: float | None = None, limit: int = 720) -> list[Sample]:
        ring = self._rings.get(node_id)
        if not ring:
            return []
        out = [s for s in ring if since is None or s.ts >= since]
        return out[-limit:]

    async def history(
        self, node_id: str, since_ms: int, until_ms: int | None = None, limit: int = 2000
    ) -> list[dict[str, Any]]:
        until_ms = until_ms if until_ms is not None else int(time.time() * 1000)
        limit = max(1, min(limit, 10000))

        def _query(db: SyncDB) -> list[dict[str, Any]]:
            rows = db.fetchall(
                "SELECT * FROM metrics_1m WHERE node_id=? AND ts_ms>=? AND ts_ms<=?"
                " ORDER BY ts_ms LIMIT ?",
                (node_id, since_ms, until_ms, limit),
            )
            out = []
            for r in rows:
                d = dict(r)
                d["extra"] = json.loads(d["extra"]) if d["extra"] else None
                out.append(d)
            return out

        return await self.db.run(_query)

    # -- rollups ---------------------------------------------------------------------------

    def _take_pending(self) -> dict[str, list[Sample]]:
        pending, self._pending = self._pending, {}
        return pending

    async def rollup(self, now: float | None = None) -> int:
        """Write one row per node summarizing the samples since the last call."""
        now = now if now is not None else time.time()
        pending = self._take_pending()
        rows = []
        bucket_ms = int(now // ROLLUP_INTERVAL_S * ROLLUP_INTERVAL_S * 1000)
        for node_id, samples in pending.items():
            if not samples:
                continue
            rows.append(
                (
                    node_id,
                    bucket_ms,
                    _avg([s.cpu for s in samples]),
                    _max([s.cpu for s in samples]),
                    _avg([s.mem for s in samples]),
                    _avg([s.temp for s in samples]),
                    _max([s.temp for s in samples]),
                    _avg([s.disk for s in samples]),
                    _int(_avg([s.net_rx_bps for s in samples])),
                    _int(_avg([s.net_tx_bps for s in samples])),
                    _avg([s.bpu for s in samples]),
                    json.dumps({"samples": len(samples)}),
                )
            )
        self._last_rollup = now
        if not rows:
            return 0

        def _write(db: SyncDB) -> None:
            with db.transaction(immediate=True):
                db.conn.executemany(
                    "INSERT OR REPLACE INTO metrics_1m (node_id, ts_ms, cpu_avg, cpu_max, mem_pct,"
                    " temp_avg, temp_max, disk_pct, net_rx, net_tx, bpu_avg, extra)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )

        await self.db.run(_write)
        return len(rows)

    async def prune(self, retention_days: int = RETENTION_DAYS) -> int:
        cutoff = int((time.time() - retention_days * 86400) * 1000)

        def _prune(db: SyncDB) -> int:
            with db.transaction(immediate=True):
                cur = db.execute("DELETE FROM metrics_1m WHERE ts_ms < ?", (cutoff,))
                return cur.rowcount

        return await self.db.run(_prune)

    def due(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now - self._last_rollup >= ROLLUP_INTERVAL_S


def _avg(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 2) if nums else None


def _max(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    return max(nums) if nums else None


def _int(value: float | None) -> int | None:
    return int(value) if value is not None else None


__all__ = ["ROLLUP_INTERVAL_S", "RETENTION_DAYS", "MetricsStore", "Sample", "summarize"]
