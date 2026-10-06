"""Wire format between cluster-agent and cluster-execd over /run/cluster-execd.sock.

One connection carries one request:
  agent -> execd   one JSON line (the request, see request.py), then optionally
                   {"op": "cancel"} lines. Closing the connection also cancels the run.
  execd -> agent   JSON lines, one event each:
    {"ev": "accepted", "run_id", "unit", "isolation", "limits"}
    {"ev": "out", "stream": "stdout"|"stderr", "data": <base64>}
    {"ev": "exit", "status", "exit_code", "reason", "duration_ms"}
    {"ev": "rejected", "reason"}
    {"ev": "info", ...}                                   (kind=info)
    {"ev": "file", "name", "size", "truncated"} {"ev": "data", "data"}... {"ev": "file_end",
     "sha256"} ... {"ev": "collected", "files", "skipped"}  (kind=collect)
    {"ev": "done"}                                        (kind=stop/cleanup)

exit.status is one of ok, error, timeout, cancelled, oom, scheduled, failed_to_start.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

EXIT_STATUSES = ("ok", "error", "timeout", "cancelled", "oom", "scheduled", "failed_to_start")
DATA_CHUNK = 48 * 1024  # raw bytes per "out"/"data" event before base64


class ClientGone(Exception):
    """The agent closed the connection."""


class EventWriter:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self._lock = asyncio.Lock()

    async def send(self, ev: str, **fields: Any) -> None:
        line = json.dumps(dict(ev=ev, **fields), separators=(",", ":")) + "\n"
        async with self._lock:
            try:
                self.writer.write(line.encode("utf-8"))
                await self.writer.drain()
            except (ConnectionError, BrokenPipeError, RuntimeError) as exc:
                raise ClientGone() from exc

    async def send_bytes(self, ev: str, data: bytes, **fields: Any) -> None:
        for i in range(0, len(data), DATA_CHUNK):
            chunk = base64.b64encode(data[i : i + DATA_CHUNK]).decode("ascii")
            await self.send(ev, data=chunk, **fields)

    def close(self) -> None:
        try:
            self.writer.close()
        except (ConnectionError, RuntimeError):
            pass
