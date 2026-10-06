"""Client for cluster-execd (/run/cluster-execd.sock). Wire format: execd/cluster_execd/protocol.py.

The agent never starts processes itself on real nodes: every run, collect and cleanup goes
through execd, which checks the node-local policy (docs/design/security.md 9.3).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import stat
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from .executor import ExecRequest, Launcher, LaunchFailed, LaunchRejected, RunHandle

DEFAULT_SOCKET = "/run/cluster-execd.sock"
STREAM_LIMIT = 1024 * 1024  # longest event line (48KB base64 chunks fit easily)
CONNECT_TIMEOUT = 5.0
_SAFE_REL = re.compile(r"[A-Za-z0-9._ +=@,-]+(/[A-Za-z0-9._ +=@,-]+)*")


class ExecdError(Exception):
    pass


class _Conn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def event(self) -> Optional[Dict[str, Any]]:
        line = await self.reader.readline()
        if not line:
            return None
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise ExecdError("malformed event from execd") from exc
        if not isinstance(event, dict) or "ev" not in event:
            raise ExecdError("malformed event from execd")
        return event

    async def send(self, obj: Dict[str, Any]) -> None:
        self.writer.write(json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n")
        await self.writer.drain()

    def close(self) -> None:
        try:
            self.writer.close()
        except (ConnectionError, RuntimeError):
            pass


class ExecdClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self.socket_path = socket_path

    async def connect(self, request: Dict[str, Any]) -> _Conn:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path, limit=STREAM_LIMIT), CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ExecdError(f"cannot reach cluster-execd: {exc}") from exc
        conn = _Conn(reader, writer)
        await conn.send(request)
        return conn

    async def _simple(self, request: Dict[str, Any], expect: str) -> Dict[str, Any]:
        conn = await self.connect(request)
        try:
            event = await conn.event()
        finally:
            conn.close()
        if event is None:
            raise ExecdError("execd closed the connection")
        if event["ev"] != expect:
            raise ExecdError(str(event.get("reason", event)))
        return event

    async def info(self) -> Dict[str, Any]:
        return await self._simple({"v": 1, "kind": "info"}, "info")

    async def stop(self, run_id: str) -> None:
        await self._simple({"v": 1, "kind": "stop", "run_id": run_id}, "done")

    async def cleanup(self, run_id: str) -> None:
        await self._simple({"v": 1, "kind": "cleanup", "run_id": run_id}, "done")

    async def collect(
        self,
        run_id: str,
        patterns: List[str],
        dest: str,
        max_files: int = 64,
        max_total_bytes: int = 64 * 1024 * 1024,
    ) -> Dict[str, Any]:
        """Stream a run's output files into dest (an agent-owned directory) and return
        {"files": [{name, size, sha256, truncated}], "skipped": [...]}.

        Names come from a process running job code's account: they are validated here and
        written with O_NOFOLLOW/O_EXCL so they can never leave dest.
        """
        request = {
            "v": 1,
            "kind": "collect",
            "run_id": run_id,
            "paths": patterns,
            "max_files": max_files,
            "max_total_bytes": max_total_bytes,
        }
        conn = await self.connect(request)
        os.makedirs(dest, mode=0o700, exist_ok=True)
        dest_fd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        files: List[Dict[str, Any]] = []
        current: Optional[Tuple[int, Dict[str, Any], Any]] = None
        total = 0
        try:
            while True:
                event = await conn.event()
                if event is None:
                    raise ExecdError("execd closed the connection during collect")
                ev = event["ev"]
                if ev == "file":
                    name = event.get("name")
                    if not isinstance(name, str) or not _safe_rel(name) or current is not None:
                        raise ExecdError("unsafe file name from collect")
                    fd = _create_beneath(dest_fd, name)
                    current = (
                        fd,
                        {"name": name, "size": 0, "truncated": bool(event.get("truncated"))},
                        hashlib.sha256(),
                    )
                elif ev == "data" and current is not None:
                    chunk = _b64(event.get("data"))
                    total += len(chunk)
                    if total > max_total_bytes:
                        raise ExecdError("collect exceeded the size limit")
                    os.write(current[0], chunk)
                    current[1]["size"] += len(chunk)
                    current[2].update(chunk)
                elif ev == "file_end" and current is not None:
                    fd, meta, digest = current
                    os.close(fd)
                    current = None
                    meta["sha256"] = digest.hexdigest()
                    if event.get("sha256") != meta["sha256"]:
                        raise ExecdError(f"checksum mismatch for {meta['name']}")
                    files.append(meta)
                elif ev == "collected":
                    return {"files": files, "skipped": event.get("skipped", [])}
                elif ev == "rejected":
                    raise ExecdError(str(event.get("reason")))
                else:
                    raise ExecdError(f"unexpected collect event {ev!r}")
        finally:
            if current is not None:
                os.close(current[0])
            os.close(dest_fd)
            conn.close()


def _b64(data: Any) -> bytes:
    if not isinstance(data, str):
        raise ExecdError("malformed data event")
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ExecdError("malformed data event") from exc


def _safe_rel(name: str) -> bool:
    return (
        len(name) <= 512
        and bool(_SAFE_REL.fullmatch(name))
        and ".." not in name.split("/")
        and "." not in name.split("/")
    )


def _create_beneath(dir_fd: int, rel: str) -> int:
    """Create rel under dir_fd without following any symlink on the way."""
    parts = rel.split("/")
    fds = [dir_fd]
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o700, dir_fd=fds[-1])
            except FileExistsError:
                pass
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
            fds.append(fd)
        fd = os.open(
            parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fds[-1]
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ExecdError(f"{rel} is not a regular file")
        return fd
    finally:
        for fd in fds[1:]:
            os.close(fd)


class ExecdLauncher(Launcher):
    def __init__(self, client: ExecdClient) -> None:
        self.client = client

    async def start(self, req: ExecRequest) -> RunHandle:
        try:
            conn = await self.client.connect(req.to_execd())
        except ExecdError as exc:
            raise LaunchFailed(str(exc)) from exc
        try:
            first = await asyncio.wait_for(conn.event(), CONNECT_TIMEOUT)
        except (ExecdError, asyncio.TimeoutError, ConnectionError) as exc:
            conn.close()
            raise LaunchFailed(f"no answer from execd: {exc}") from exc
        if first is None:
            conn.close()
            raise LaunchFailed("execd closed the connection")
        if first["ev"] == "rejected":
            conn.close()
            raise LaunchRejected(str(first.get("reason")))
        if first["ev"] != "accepted":
            conn.close()
            raise LaunchFailed(f"unexpected execd event {first['ev']!r}")
        return _ExecdRun(conn)


class _ExecdRun(RunHandle):
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.queue: asyncio.Queue[Optional[Tuple[str, bytes]]] = asyncio.Queue()
        self.exit: asyncio.Future[Tuple[str, Optional[int], Optional[str]]] = (
            asyncio.get_running_loop().create_future()
        )
        self.reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        outcome: Tuple[str, Optional[int], Optional[str]] = ("error", None, "execd connection lost")
        try:
            while True:
                event = await self.conn.event()
                if event is None:
                    break
                if event["ev"] == "out" and event.get("stream") in ("stdout", "stderr"):
                    await self.queue.put((event["stream"], _b64(event.get("data"))))
                elif event["ev"] == "exit":
                    code = event.get("exit_code")
                    outcome = (
                        str(event.get("status", "error")),
                        code if isinstance(code, int) else None,
                        event.get("reason"),
                    )
                    break
        except (ExecdError, ConnectionError, asyncio.LimitOverrunError, ValueError) as exc:
            outcome = ("error", None, f"execd stream error: {exc}")
        finally:
            await self.queue.put(None)
            if not self.exit.done():
                self.exit.set_result(outcome)

    async def output(self) -> AsyncIterator[Tuple[str, bytes]]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def result(self) -> Tuple[str, Optional[int], Optional[str]]:
        return await asyncio.shield(self.exit)

    async def cancel(self) -> None:
        try:
            await self.conn.send({"op": "cancel"})
        except (ConnectionError, RuntimeError):
            self.conn.close()  # closing the connection cancels too

    async def close(self) -> None:
        self.reader.cancel()
        self.conn.close()
