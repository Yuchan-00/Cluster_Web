"""cluster-execd entry point.

Production: socket-activated per connection (cluster-execd.socket, Accept=yes); systemd hands
the connected socket over as fd 3. Development and tests: --listen PATH runs a plain server.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pwd
import socket
import sys
from typing import List, Optional

from .policy import DEFAULT_PATH, PolicyError, load_policy
from .runner import Context, resolve_isolation
from .server import STREAM_LIMIT, handle_connection, peer_uid
from .units import Paths

SD_LISTEN_FDS_START = 3


def _activated_socket() -> Optional[socket.socket]:
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        return None
    for key in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(key, None)  # children must not think they were activated
    return socket.socket(fileno=SD_LISTEN_FDS_START)


async def _serve_one(sock: socket.socket, ctx: Context, allowed: List[int]) -> None:
    uid = peer_uid(sock)
    reader, writer = await asyncio.open_unix_connection(sock=sock, limit=STREAM_LIMIT)
    await handle_connection(reader, writer, ctx, uid, allowed)


async def _serve_forever(path: str, ctx: Context, allowed: List[int], mode: int) -> None:
    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = peer_uid(writer.get_extra_info("socket"))
        await handle_connection(reader, writer, ctx, uid, allowed)

    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(on_connect, path, limit=STREAM_LIMIT)
    os.chmod(path, mode)
    async with server:
        await server.serve_forever()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="cluster-execd", description=__doc__.splitlines()[0])
    parser.add_argument("--policy", default=DEFAULT_PATH)
    parser.add_argument(
        "--agent-user", default="cluster-agent", help="the only account allowed to connect"
    )
    parser.add_argument("--run-user", default="cluster-run")
    parser.add_argument(
        "--check-policy", action="store_true", help="validate the policy file and print its summary"
    )
    parser.add_argument("--listen", metavar="PATH", help="development: serve on a socket")
    parser.add_argument("--run-root", default=Paths.run_root)
    parser.add_argument("--state-dir", default=Paths.state_dir)
    parser.add_argument(
        "--insecure-dev",
        action="store_true",
        help="tests only: skip policy ownership checks and uid switching",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="cluster-execd: %(levelname)s %(message)s"
    )

    try:
        policy = load_policy(args.policy, require_root_owned=not args.insecure_dev)
    except PolicyError as exc:
        logging.error("%s", exc)
        return 2
    if args.check_policy:
        print(json.dumps(policy.summary(), indent=2, sort_keys=True))
        return 0

    paths = Paths(run_root=args.run_root, state_dir=args.state_dir)
    ctx = Context(
        policy=policy,
        paths=paths,
        run_user=args.run_user,
        isolation=resolve_isolation(policy, paths),
        switch_user=not args.insecure_dev,
    )
    if args.insecure_dev:
        allowed = [os.getuid()]
    else:
        if os.geteuid() != 0:
            logging.error("cluster-execd must run as root")
            return 2
        allowed = [pwd.getpwnam(args.agent_user).pw_uid]

    if args.listen:
        asyncio.run(_serve_forever(args.listen, ctx, allowed, 0o660))
        return 0
    sock = _activated_socket()
    if sock is None:
        logging.error("no socket from systemd (use cluster-execd.socket or --listen)")
        return 2
    asyncio.run(_serve_one(sock, ctx, allowed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
