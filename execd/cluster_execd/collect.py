"""kind=collect: read outputs from a work directory as cluster-run and stream them to the agent.

execd never writes into agent-owned directories (a compromised agent could plant symlinks
there); the agent stores the streamed files itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List

from .protocol import DATA_CHUNK, EventWriter
from .request import CollectPlan
from .runner import SAFE_PATH, Context

_SAFE_REL = re.compile(r"^[A-Za-z0-9._ +=@,-]+(/[A-Za-z0-9._ +=@,-]+)*$")
_HEADER_MAX = 4096


def worker_argv(plan: CollectPlan, ctx: Context) -> List[str]:
    spec = json.dumps(
        {
            "patterns": plan.patterns,
            "max_files": plan.max_files,
            "max_total_bytes": plan.max_total_bytes,
            "max_file_bytes": plan.max_file_bytes,
        }
    )
    # Run the worker file directly (stdlib only): -I keeps the work directory and the
    # environment out of its import path, and the child needs no package on sys.path.
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect_worker.py")
    argv = [sys.executable, "-I", worker, ctx.paths.workdir(plan.run_id), spec]
    if ctx.switch_user:
        argv = [
            "setpriv",
            f"--reuid={ctx.run_user}",
            f"--regid={ctx.run_user}",
            "--clear-groups",
            "--no-new-privs",
            "--",
        ] + argv
    return argv


def _safe_name(rel: Any) -> bool:
    return (
        isinstance(rel, str)
        and len(rel) <= 512
        and bool(_SAFE_REL.fullmatch(rel))
        and ".." not in rel.split("/")
    )


async def run_collect(plan: CollectPlan, ctx: Context, events: EventWriter) -> None:
    workdir = ctx.paths.workdir(plan.run_id)
    if not os.path.isdir(workdir) or os.path.islink(workdir):
        await events.send("rejected", reason="no such run directory")
        return
    env = {"PATH": SAFE_PATH, "LANG": "C.UTF-8"}
    proc = await asyncio.create_subprocess_exec(
        *worker_argv(plan, ctx),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        cwd="/",
        limit=_HEADER_MAX,
    )
    if proc.stdout is None:
        raise RuntimeError("collect worker has no stdout pipe")
    files: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    total = 0
    error = None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                error = "worker ended without end marker"
                break
            header = json.loads(line)
            if header.get("end") is True:
                break
            if "skip" in header:
                name = header["skip"] if _safe_name(header["skip"]) else "(unsafe name)"
                skipped.append({"name": name, "reason": str(header.get("reason"))[:64]})
                continue
            name, size = header.get("file"), header.get("size")
            # The worker runs as cluster-run, so treat its framing as untrusted too.
            if not _safe_name(name) or not isinstance(size, int) or size < 0:
                error = "malformed frame"
                break
            if (
                size > plan.max_file_bytes
                or total + size > plan.max_total_bytes
                or len(files) >= plan.max_files
            ):
                error = "worker exceeded limits"
                break
            total += size
            await events.send("file", name=name, size=size, truncated=bool(header.get("truncated")))
            digest = hashlib.sha256()
            remaining = size
            while remaining:  # stream in chunks: a Pi cannot hold a 64MB artifact in memory
                chunk = await proc.stdout.readexactly(min(DATA_CHUNK, remaining))
                digest.update(chunk)
                remaining -= len(chunk)
                await events.send_bytes("data", chunk)
            await events.send("file_end", sha256=digest.hexdigest())
            files.append({"name": name, "size": size})
    except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        error = f"malformed frame: {type(exc).__name__}"
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
    if error:
        await events.send("rejected", reason=f"collect failed: {error}")
        return
    await events.send("collected", files=files, skipped=skipped)
