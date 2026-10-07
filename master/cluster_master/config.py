"""Master configuration: /etc/cluster-master/config.yaml.

Unknown keys are errors (a mistyped security setting must not fall back to a default), and the
development-only shortcuts refuse to combine with anything that is reachable from the network.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field, fields
from typing import Any
from urllib.parse import urlparse

import yaml

DEFAULT_PATH = "/etc/cluster-master/config.yaml"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class ConfigError(ValueError):
    pass


@dataclass
class TcpListener:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class UdsListener:
    path: str = ""
    mode: int = 0o660
    group: str | None = None  # group that may connect (cluster-svc for internal)


@dataclass
class Listeners:
    # security.md 4.1: web and agent on loopback only (tailscale serve / Caddy in front),
    # internal for the service processes, admin for the root CLI.
    web: TcpListener = field(default_factory=lambda: TcpListener("127.0.0.1", 8000))
    agent: TcpListener = field(default_factory=lambda: TcpListener("127.0.0.1", 8001))
    internal: UdsListener = field(
        default_factory=lambda: UdsListener(
            "/run/cluster-master/internal.sock", 0o660, "cluster-svc"
        )
    )
    admin: UdsListener = field(
        default_factory=lambda: UdsListener("/run/cluster-master/admin.sock", 0o600, None)
    )


@dataclass
class AgentLimits:
    """security.md 8.3 input limits and PLAN.md 12.1 timings."""

    metrics_interval_s: float = 5.0
    offline_after_s: float = 15.0
    hello_timeout_s: float = 10.0
    ws_max_bytes: int = 1024 * 1024
    output_chunk_max_bytes: int = 64 * 1024
    msgs_per_s: float = 50.0
    bytes_per_s: int = 1024 * 1024
    metrics_min_interval_s: float = 2.0
    metrics_max_bytes: int = 64 * 1024  # one metrics message, serialized
    extra_max_bytes: int = 4096
    extra_max_keys: int = 64
    static_info_max_bytes: int = 16 * 1024
    ring_size: int = 720  # 1 hour of 5 s samples kept in memory per node
    lease_ttl_s: float = 45.0
    work_request_interval_s: float = 5.0


@dataclass
class WebSettings:
    origins: list[str] = field(default_factory=list)
    base_url: str = ""


@dataclass
class DevSettings:
    """Development shortcuts. Each one is refused unless the web listener is loopback-only."""

    # Phase 2 (before the Phase 3 login): every web request acts as the admin user "dev".
    unauthenticated_admin: bool = False
    # Accept ws:// agents from the mock cluster without Caddy in front: that is always the
    # case for the agent listener (TLS is Caddy's job), so this flag only documents intent.
    mock_cluster: bool = False


@dataclass
class MasterConfig:
    data_dir: str = "/var/lib/cluster-master"
    db_path: str = ""  # default: <data_dir>/master.db
    credentials_dir: str = (
        ""  # default: $CREDENTIALS_DIRECTORY, else /etc/cluster-master/credentials
    )
    log_level: str = "INFO"
    listeners: Listeners = field(default_factory=Listeners)
    agent: AgentLimits = field(default_factory=AgentLimits)
    web: WebSettings = field(default_factory=WebSettings)
    dev: DevSettings = field(default_factory=DevSettings)

    def resolve(self) -> None:
        if not self.db_path:
            self.db_path = os.path.join(self.data_dir, "master.db")
        if not self.credentials_dir:
            self.credentials_dir = os.environ.get(
                "CREDENTIALS_DIRECTORY", "/etc/cluster-master/credentials"
            )

    def validate(self) -> None:
        for name in ("web", "agent"):
            listener: TcpListener = getattr(self.listeners, name)
            if not 1 <= listener.port <= 65535:
                raise ConfigError(f"listeners.{name}.port out of range")
            if not _is_loopback(listener.host):
                # security.md 4.2: TLS termination and exposure belong to Caddy / tailscale serve.
                raise ConfigError(
                    f"listeners.{name}.host must be a loopback address; expose it through "
                    "tailscale serve or Caddy, never directly"
                )
        for name in ("internal", "admin"):
            uds: UdsListener = getattr(self.listeners, name)
            if not uds.path.startswith("/"):
                raise ConfigError(f"listeners.{name}.path must be absolute")
        a = self.agent
        if not 1 <= a.metrics_interval_s <= 60:
            raise ConfigError("agent.metrics_interval_s must be between 1 and 60")
        if a.offline_after_s < 2 * a.metrics_interval_s:
            raise ConfigError("agent.offline_after_s must be at least twice metrics_interval_s")
        if a.ws_max_bytes < 64 * 1024 or a.output_chunk_max_bytes > a.ws_max_bytes:
            raise ConfigError("agent websocket size limits are inconsistent")
        if a.metrics_max_bytes < 4096 or a.metrics_max_bytes > a.ws_max_bytes:
            raise ConfigError("agent.metrics_max_bytes must be between 4 KiB and ws_max_bytes")
        if a.metrics_interval_s < 2 * a.metrics_min_interval_s:
            # welcome tells agents the interval; the hub refuses anything faster than min
            raise ConfigError(
                "agent.metrics_interval_s must be at least twice metrics_min_interval_s"
            )
        if a.ring_size < 1:
            raise ConfigError("agent.ring_size must be positive")
        for origin in self.web.origins:
            url = urlparse(origin)
            if url.scheme not in ("https", "http") or not url.netloc or url.path not in ("", "/"):
                raise ConfigError(f"web.origins entry is not an origin: {origin!r}")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError("log_level must be DEBUG, INFO, WARNING or ERROR")
        if self.dev.unauthenticated_admin and not _is_loopback(self.listeners.web.host):
            raise ConfigError("dev.unauthenticated_admin needs a loopback web listener")


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _build(cls: type, data: Any, where: str) -> Any:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be a mapping")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")
    kwargs = {}
    for key, value in data.items():
        target = known[key].type
        nested = _NESTED.get((cls, key))
        if nested is not None:
            kwargs[key] = _build(nested, value, f"{where}.{key}")
        elif key == "mode" and isinstance(value, str):
            kwargs[key] = int(value, 8)
        else:
            if isinstance(value, bool) and target in ("int", "float", "str"):
                raise ConfigError(f"{where}.{key} has the wrong type")
            kwargs[key] = value
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


_NESTED = {
    (MasterConfig, "listeners"): Listeners,
    (MasterConfig, "agent"): AgentLimits,
    (MasterConfig, "web"): WebSettings,
    (MasterConfig, "dev"): DevSettings,
    (Listeners, "web"): TcpListener,
    (Listeners, "agent"): TcpListener,
    (Listeners, "internal"): UdsListener,
    (Listeners, "admin"): UdsListener,
}


def parse_config(data: dict[str, Any] | None) -> MasterConfig:
    cfg: MasterConfig = _build(MasterConfig, data, "config")
    cfg.resolve()
    cfg.validate()
    return cfg


def load_config(path: str = DEFAULT_PATH) -> MasterConfig:
    try:
        with open(path, encoding="utf-8") as f:
            return parse_config(yaml.safe_load(f))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
