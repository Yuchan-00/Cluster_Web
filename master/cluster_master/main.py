"""`cluster-master`: run the four listeners in one process.

The UNIX sockets are bound here, not by uvicorn, so that they never exist with a mode wider
than the configured one (uvicorn would chmod them 0666 first). Shutdown closes every agent
and browser connection, flushes the metrics rollup and closes the database.
"""

from __future__ import annotations

import argparse
import asyncio
import grp
import logging
import os
import signal
import socket
import stat
import sys
import time
from collections.abc import Sequence

import uvicorn
from cluster_common.redact import RedactingFilter, default_redactor
from fastapi import FastAPI

from . import __version__
from .app import build_apps
from .config import DEFAULT_PATH, ConfigError, MasterConfig, TcpListener, UdsListener, load_config
from .services.metrics_store import ROLLUP_INTERVAL_S
from .state import AppState

log = logging.getLogger("cluster_master")
UI_WS_MAX_BYTES = 64 * 1024


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(default_redactor))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def bind_uds(listener: UdsListener) -> socket.socket:
    path = listener.path
    if os.path.lexists(path):
        if not stat.S_ISSOCK(os.lstat(path).st_mode):
            raise ConfigError(f"{path} exists and is not a socket")
        os.unlink(path)
    os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o777 & ~listener.mode)
    try:
        sock.bind(path)
    finally:
        os.umask(old_umask)
    os.chmod(path, listener.mode)
    if listener.group:
        try:
            os.chown(path, -1, grp.getgrnam(listener.group).gr_gid)
        except (KeyError, PermissionError) as exc:
            log.warning("cannot chgrp %s to %s: %s", path, listener.group, exc)
    sock.listen(128)
    sock.setblocking(False)
    return sock


def bind_tcp(listener: TcpListener) -> socket.socket:
    family = socket.AF_INET6 if ":" in listener.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((listener.host, listener.port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def make_server(app: FastAPI, *, ws_max_size: int, proxy_headers: bool) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        log_config=None,
        access_log=False,
        lifespan="off",
        ws_max_size=ws_max_size,
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        proxy_headers=proxy_headers,
        forwarded_allow_ips="127.0.0.1,::1",
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=5,
    )
    config.load()
    server = uvicorn.Server(config)
    # Server.serve() would do this; we drive startup()/main_loop()/shutdown() ourselves so
    # four servers can share one loop and one signal handler.
    server.lifespan = config.lifespan_class(config)
    return server


async def serve(cfg: MasterConfig) -> int:
    state = AppState.build(cfg)
    await state.start()
    apps = build_apps(state)
    plan = [
        (apps.web, bind_tcp(cfg.listeners.web), UI_WS_MAX_BYTES, True),
        (apps.agent, bind_tcp(cfg.listeners.agent), cfg.agent.ws_max_bytes, True),
        (apps.internal, bind_uds(cfg.listeners.internal), UI_WS_MAX_BYTES, False),
        (apps.admin, bind_uds(cfg.listeners.admin), UI_WS_MAX_BYTES, False),
    ]
    servers = []
    for app, sock, ws_max, proxy in plan:
        server = make_server(app, ws_max_size=ws_max, proxy_headers=proxy)
        await server.startup(sockets=[sock])
        servers.append((server, sock))

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal(sig: int) -> None:
        log.info("received %s, shutting down", signal.Signals(sig).name)
        stop.set()
        for server, _ in servers:
            server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal, sig)

    housekeeping = asyncio.create_task(_housekeeping(state, stop), name="housekeeping")
    log.info(
        "cluster-master %s listening: web %s:%d, agent %s:%d, internal %s, admin %s%s",
        __version__,
        cfg.listeners.web.host,
        cfg.listeners.web.port,
        cfg.listeners.agent.host,
        cfg.listeners.agent.port,
        cfg.listeners.internal.path,
        cfg.listeners.admin.path,
        " [DEV ADMIN]" if cfg.dev.unauthenticated_admin else "",
    )
    try:
        await asyncio.gather(*(server.main_loop() for server, _ in servers))
    finally:
        stop.set()
        housekeeping.cancel()
        try:
            await housekeeping
        except asyncio.CancelledError:
            pass
        # 1. tell every agent/browser we are going away, 2. let uvicorn drain the handlers
        # (they may still write last_seen/alerts), 3. only then flush and close the database.
        await state.disconnect_all()
        for server, sock in servers:
            await server.shutdown(sockets=[sock])
        await state.close()
        for listener in (cfg.listeners.internal, cfg.listeners.admin):
            try:
                os.unlink(listener.path)
            except OSError:
                pass
    return 0


PRUNE_INTERVAL_S = 24 * 3600.0


async def _housekeeping(state: AppState, stop: asyncio.Event) -> None:
    """Minute rollups; retention prune at startup and then daily by the clock (a master that
    restarts more often than daily would otherwise never prune)."""
    last_prune = 0.0
    while not stop.is_set():
        try:
            if time.time() - last_prune >= PRUNE_INTERVAL_S:
                removed = await state.metrics.prune()
                last_prune = time.time()
                if removed:
                    log.info("pruned %d metrics_1m row(s) past retention", removed)
        except Exception:  # noqa: BLE001
            log.exception("metrics prune failed")
        try:
            await asyncio.wait_for(stop.wait(), ROLLUP_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await state.metrics.rollup()
        except Exception:  # noqa: BLE001
            log.exception("metrics rollup failed")


def dev_config(data_dir: str, web_port: int, agent_port: int) -> MasterConfig:
    """Loopback development master for the mock cluster (scripts/dev_cluster.sh)."""
    data_dir = os.path.abspath(data_dir)
    run_dir = os.path.join(data_dir, "run")
    cfg = MasterConfig(data_dir=data_dir, log_level="INFO")
    cfg.listeners.web.port = web_port
    cfg.listeners.agent.port = agent_port
    cfg.listeners.internal = UdsListener(os.path.join(run_dir, "internal.sock"), 0o600, None)
    cfg.listeners.admin = UdsListener(os.path.join(run_dir, "admin.sock"), 0o600, None)
    cfg.dev.unauthenticated_admin = True
    cfg.dev.mock_cluster = True
    cfg.resolve()
    cfg.validate()
    return cfg


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cluster-master", description="Cluster Web master")
    parser.add_argument("--config", default=DEFAULT_PATH)
    parser.add_argument("--check", action="store_true", help="validate the config and exit")
    parser.add_argument(
        "--dev",
        metavar="DATA_DIR",
        help="development mode: loopback listeners, unauthenticated admin, state in DATA_DIR",
    )
    parser.add_argument("--dev-web-port", type=int, default=8000)
    parser.add_argument("--dev-agent-port", type=int, default=8001)
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        if args.dev:
            cfg = dev_config(args.dev, args.dev_web_port, args.dev_agent_port)
        else:
            cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"cluster-master: {exc}", file=sys.stderr)
        return 2
    if args.check:
        print("config ok")
        return 0
    setup_logging(cfg.log_level)
    try:
        return asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
