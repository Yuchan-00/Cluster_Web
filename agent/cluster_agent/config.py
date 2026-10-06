"""Agent configuration (/etc/cluster-agent/config.yaml).

Unknown keys are an error: a typo in a security setting must not silently fall back to a default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import yaml

from .collectors import BOARDS

DEFAULT_PATH = "/etc/cluster-agent/config.yaml"

_NODE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_LABEL_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CAPACITY_KEYS = {"slots", "bpu_slots", "job_mem_mb"}


class ConfigError(ValueError):
    pass


@dataclass
class CommandLimits:
    max_concurrent: int = 2
    max_output_bytes: int = 1024 * 1024
    default_timeout: float = 60.0
    max_timeout: float = 600.0
    kill_grace: float = 5.0


@dataclass
class AgentConfig:
    node_id: str
    master_url: str
    board: str = "auto"
    token_file: str = "/etc/cluster-agent/token"  # noqa: S105 - a path, not a secret
    ca_file: str = "/etc/cluster-agent/master-ca.pem"
    metrics_interval: float = 5.0
    slow_every: int = 3
    labels: Dict[str, str] = field(default_factory=dict)
    capacity: Dict[str, int] = field(default_factory=dict)
    commands: CommandLimits = field(default_factory=CommandLimits)
    # Development only: allows ws:// to a loopback master (mock clusters on one PC).
    allow_insecure_loopback: bool = False

    def validate(self) -> None:
        if not _NODE_ID.match(self.node_id):
            raise ConfigError("node_id must be lowercase letters, digits and '-' (max 63)")
        url = urlparse(self.master_url)
        loopback = url.hostname in ("localhost", "127.0.0.1", "::1")
        if url.scheme != "wss" and not (
            url.scheme == "ws" and loopback and self.allow_insecure_loopback
        ):
            raise ConfigError("master_url must use wss:// (ws:// only for loopback in dev mode)")
        if not url.hostname:
            raise ConfigError("master_url has no host")
        if self.board not in BOARDS:
            raise ConfigError(f"board must be one of {', '.join(BOARDS)}")
        if not 1 <= self.metrics_interval <= 60:
            raise ConfigError("metrics_interval must be between 1 and 60 seconds")
        if not 1 <= self.slow_every <= 60:
            raise ConfigError("slow_every must be between 1 and 60")
        for key, value in self.labels.items():
            if not _LABEL_KEY.match(str(key)) or not _LABEL_VALUE.match(str(value)):
                raise ConfigError(f"invalid label {key!r}={value!r}")
        for key, value in self.capacity.items():
            if key not in _CAPACITY_KEYS:
                raise ConfigError(f"unknown capacity key {key!r}")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ConfigError(f"capacity {key} must be a non-negative integer")
        c = self.commands
        if not (1 <= c.max_concurrent <= 16 and 0 < c.default_timeout <= c.max_timeout):
            raise ConfigError("invalid commands limits")


def _build(cls: Any, data: Dict[str, Any], where: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be a mapping")
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")
    return cls(**data)


def parse_config(data: Optional[Dict[str, Any]]) -> AgentConfig:
    data = dict(data or {})
    for required in ("node_id", "master_url"):
        if required not in data:
            raise ConfigError(f"missing required key: {required}")
    if "commands" in data:
        data["commands"] = _build(CommandLimits, data["commands"], "commands")
    try:
        cfg = _build(AgentConfig, data, "config")
    except TypeError as exc:
        raise ConfigError(str(exc)) from exc
    cfg.validate()
    return cfg


def load_config(path: str = DEFAULT_PATH) -> AgentConfig:
    try:
        with open(path, encoding="utf-8") as f:
            return parse_config(yaml.safe_load(f))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
