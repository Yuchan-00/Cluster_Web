"""cluster-agent entry point.

  cluster-agent                         run with /etc/cluster-agent/config.yaml
  cluster-agent --once                  print static info and one metrics sample as JSON
  cluster-agent --mock rpi3 --name rpi3-01 --master ws://127.0.0.1:8001/ws/agent \\
                --token-file dev.token  simulated node for developing the master and web UI
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .agent import Agent
from .collectors import MetricsCollector
from .config import DEFAULT_PATH, AgentConfig, ConfigError, load_config
from .connection import load_token, make_ssl_context
from .execd_client import DEFAULT_SOCKET, ExecdClient, ExecdError, ExecdLauncher
from .executor import DirectLauncher, Executor
from .labels import build_capacity, build_labels

log = logging.getLogger("cluster_agent")


def _static_extra(
    collector: MetricsCollector, labels_cfg: Dict[str, str], capacity_cfg: Dict[str, int]
) -> Dict[str, Any]:
    info = collector.static_info()
    labels = build_labels(collector.board, info, labels_cfg)
    return {"labels": labels, "capacity": build_capacity(collector.board, labels, capacity_cfg)}


def cmd_once(args: argparse.Namespace) -> int:
    board = args.mock or "auto"
    if args.config and not args.mock:
        try:
            board = load_config(args.config).board
        except ConfigError:
            pass  # --once is a diagnostic: fall back to auto-detection
    collector = MetricsCollector(board=board, mock_name=args.name if args.mock else None)
    collector.collect()  # prime counters so rates and CPU percentages are meaningful
    time.sleep(1.0)
    out = {
        "static_info": collector.static_info(),
        **_static_extra(collector, {}, {}),
        "metrics": collector.collect(),
    }
    print(json.dumps(out, indent=2, sort_keys=True, default=str))
    return 0


async def _amain(args: argparse.Namespace) -> int:
    # Build inside the running loop: on Python 3.8/3.9 asyncio primitives bind to the loop
    # that exists when they are created.
    try:
        agent = (
            build_mock(args)
            if args.mock
            else await build_real(load_config(args.config), args.execd_socket)
        )
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(agent.stop()))
    await agent.run_forever()
    return 0


async def _execd_info(client: ExecdClient) -> Dict[str, Any]:
    try:
        info = await client.info()
    except ExecdError as exc:
        log.warning("cluster-execd unavailable (%s); commands will fail until it is", exc)
        return {"isolation": "unavailable"}
    return {
        "isolation": info.get("isolation"),
        "execd_version": info.get("version"),
        "cgroup_controllers": info.get("controllers"),
        "node_policy": info.get("policy"),
    }


async def build_real(cfg: AgentConfig, socket_path: str) -> Agent:
    token = load_token(cfg.token_file)
    ssl_ctx = make_ssl_context(cfg.ca_file)
    collector = MetricsCollector(board=cfg.board, slow_every=cfg.slow_every)
    client = ExecdClient(socket_path)
    executor = Executor(
        ExecdLauncher(client),
        max_concurrent=cfg.commands.max_concurrent,
        max_output_bytes=cfg.commands.max_output_bytes,
    )
    extra = _static_extra(collector, cfg.labels, cfg.capacity)
    extra.update(await _execd_info(client))
    return Agent(
        cfg.node_id,
        cfg.master_url,
        token,
        collector,
        executor,
        static_extra=extra,
        ssl_ctx=ssl_ctx,
        metrics_interval=cfg.metrics_interval,
        command_limits=cfg.commands,
    )


def build_mock(args: argparse.Namespace) -> Agent:
    url = urlparse(args.master or "")
    loopback = url.hostname in ("localhost", "127.0.0.1", "::1")
    if url.scheme == "ws" and not loopback:
        raise ConfigError("mock mode allows ws:// only to a loopback master")
    if url.scheme not in ("ws", "wss"):
        raise ConfigError("--master must be a ws:// or wss:// URL")
    if not args.name or not args.token_file:
        raise ConfigError("mock mode needs --name and --token-file")
    log.warning("MOCK NODE %s: commands run unisolated as the current user", args.name)
    collector = MetricsCollector(board=args.mock, mock_name=args.name)
    executor = Executor(DirectLauncher())
    extra = _static_extra(collector, {}, {})
    extra["isolation"] = "mock"
    ssl_ctx = make_ssl_context(args.ca_file) if url.scheme == "wss" else None
    return Agent(
        args.name,
        args.master,
        load_token(args.token_file),
        collector,
        executor,
        static_extra=extra,
        ssl_ctx=ssl_ctx,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cluster-agent",
        description="Cluster Web node agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=DEFAULT_PATH)
    parser.add_argument("--once", action="store_true", help="print one sample and exit")
    parser.add_argument("--execd-socket", default=DEFAULT_SOCKET)
    parser.add_argument("--mock", choices=["rpi3", "rdkx3", "odroidn2"], help="simulate a node")
    parser.add_argument("--name", help="mock node name")
    parser.add_argument("--master", help="mock: master URL")
    parser.add_argument("--token-file", help="mock: token file")
    parser.add_argument("--ca-file", help="mock: CA file for wss://")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(name)s: %(levelname)s %(message)s",
    )

    if args.once:
        return cmd_once(args)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
