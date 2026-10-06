"""Connection handling: peer check, request parsing and dispatch (one request per connection)."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time
from typing import Iterable, Optional

from . import __version__
from .collect import run_collect
from .policy import RequestError
from .protocol import ClientGone, EventWriter
from .request import MAX_REQUEST_BYTES, CollectPlan, ExecPlan, SimplePlan, parse_request
from .runner import (
    Context,
    cgroup_controllers,
    remove_workdir,
    run_exec,
    stop_fallback_run,
    stop_unit,
)
from .units import unit_name

log = logging.getLogger(__name__)

STREAM_LIMIT = MAX_REQUEST_BYTES + 2
REQUEST_TIMEOUT = 10.0


def peer_uid(sock: socket.socket) -> int:
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


async def _watch_client(reader: asyncio.StreamReader, cancel: asyncio.Event) -> None:
    """A {"op":"cancel"} line or the agent going away both cancel the run."""
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                if json.loads(line).get("op") == "cancel":
                    break
            except (ValueError, AttributeError):
                continue
    except (ConnectionError, asyncio.LimitOverrunError, ValueError):
        pass
    cancel.set()


async def _dispatch(
    plan: object, ctx: Context, events: EventWriter, reader: asyncio.StreamReader
) -> Optional[str]:
    if isinstance(plan, ExecPlan):
        await events.send(
            "accepted",
            run_id=plan.run_id,
            unit=unit_name(plan.run_id),
            isolation=ctx.isolation,
            limits=plan.limits.__dict__,
        )
        cancel = asyncio.Event()
        watcher = asyncio.ensure_future(_watch_client(reader, cancel))
        started = time.monotonic()
        try:
            outcome = await run_exec(plan, ctx, events, cancel)
        finally:
            watcher.cancel()
        await events.send(
            "exit",
            status=outcome.status,
            exit_code=outcome.exit_code,
            reason=outcome.reason,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return outcome.status
    if isinstance(plan, CollectPlan):
        await run_collect(plan, ctx, events)
        return "collected"
    if not isinstance(plan, SimplePlan):
        raise TypeError(f"unexpected plan {type(plan).__name__}")
    if plan.kind == "info":
        await events.send(
            "info",
            version=__version__,
            isolation=ctx.isolation,
            controllers=cgroup_controllers(),
            policy=ctx.policy.summary(),
        )
    elif plan.kind == "stop":
        if ctx.isolation == "systemd":
            await stop_unit(plan.run_id or "", ctx)
        else:
            stop_fallback_run(ctx, plan.run_id or "")
        await events.send("done")
    elif plan.kind == "cleanup":
        remove_workdir(ctx, plan.run_id or "")
        await events.send("done")
    return plan.kind


async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ctx: Context,
    uid: int,
    allowed_uids: Iterable[int],
) -> None:
    events = EventWriter(writer)
    try:
        try:
            # Read the request even from a stranger: closing with unread data makes the
            # kernel reset the connection and the peer would never see the rejection.
            line = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT)
            if uid not in set(allowed_uids):
                log.warning("rejected connection from uid %d", uid)
                await events.send("rejected", reason="peer not allowed")
                return
            plan = parse_request(line, ctx.policy)
        except (ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError) as exc:
            # RequestError is a ValueError; a too-long line raises ValueError from readline.
            reason = str(exc) if isinstance(exc, RequestError) else "unreadable request"
            log.info("rejected request: %s", reason)
            await events.send("rejected", reason=reason)
            return
        result = await _dispatch(plan, ctx, events, reader)
        log.info("%s %s -> %s", type(plan).__name__, getattr(plan, "run_id", "-"), result)
    except ClientGone:
        log.info("agent disconnected")
    except Exception:  # noqa: BLE001 - never leak a traceback to the peer, keep a log
        log.exception("request failed")
        try:
            await events.send("rejected", reason="internal error")
        except ClientGone:
            pass
    finally:
        events.close()
