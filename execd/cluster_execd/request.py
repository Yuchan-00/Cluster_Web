"""Validation of agent requests into execution plans (docs/design/security.md 9.3).

Everything that reaches systemd-run or exec() comes out of this module; the agent is treated
as untrusted input (it runs a network-facing daemon).
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .policy import Limits, Policy, RequestError

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 256 * 1024
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
EXEC_KINDS = ("command", "root_op")
OTHER_KINDS = ("stop", "collect", "cleanup", "info")
NOT_YET = ("job", "install_bundle")  # Phase 7 (docs/PLAN.md section 18)
NETWORKS = ("internet", "lan", "none")
DEFAULT_TIMEOUT_S = 60
SHELL = "/bin/sh"

_ENV_FIXED = {"LANG", "TZ", "PYTHONDONTWRITEBYTECODE", "HOME"}
_ENV_USER = re.compile(r"^CW_[A-Z0-9_]{1,64}$")
_ENV_VALUE_MAX = 4096
_ENV_MAX_KEYS = 64
_ARG_MAX = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_COLLECT_PATTERN = re.compile(r"^[A-Za-z0-9._*?/\[\]-]{1,256}$")


@dataclass
class ExecPlan:
    """A validated command or root_op, ready to be turned into a unit or a process."""

    run_id: str
    kind: str  # command | root_op
    argv: List[str]
    as_root: bool
    limits: Limits
    network: str
    env: Dict[str, str]
    detach: bool = False
    survive_disconnect: bool = False
    root_op: Optional[str] = None

    @property
    def sandboxed(self) -> bool:
        """Runs as cluster-run inside the systemd sandbox (security.md 11.2)."""
        return not self.as_root


@dataclass
class CollectPlan:
    run_id: str
    patterns: List[str]
    max_files: int = 64
    max_total_bytes: int = 64 * 1024 * 1024
    max_file_bytes: int = 64 * 1024 * 1024

    def matches(self, relpath: str) -> bool:
        return any(fnmatch.fnmatchcase(relpath, p) for p in self.patterns)


@dataclass
class SimplePlan:
    kind: str  # stop | cleanup | info
    run_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def decode_request(line: bytes) -> Dict[str, Any]:
    if len(line) > MAX_REQUEST_BYTES:
        raise RequestError("request too large")
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RequestError(f"request is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RequestError("request must be a JSON object")
    if data.get("v") != PROTOCOL_VERSION:
        raise RequestError(f"unsupported protocol version {data.get('v')!r}")
    return data


def _run_id(data: Dict[str, Any]) -> str:
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise RequestError("run_id must match ^[A-Za-z0-9_-]{1,64}$")
    return run_id


def _clean_str(value: Any, what: str, limit: int = _ARG_MAX, allow_newline: bool = False) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{what} must be a string")
    if len(value.encode("utf-8", "surrogatepass")) > limit:
        raise RequestError(f"{what} is too long")
    if "\x00" in value:
        raise RequestError(f"{what} contains a NUL byte")
    if not allow_newline and _CONTROL.search(value):
        raise RequestError(f"{what} contains control characters")
    return value


def _limits(raw: Any, cap: Limits) -> Limits:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise RequestError("limits must be an object")
    unknown = set(raw) - {"memory_mb", "cpu_pct", "tasks", "timeout_s"}
    if unknown:
        raise RequestError(f"unknown limit(s): {', '.join(sorted(unknown))}")
    defaults = {
        "memory_mb": cap.memory_mb,
        "cpu_pct": cap.cpu_pct,
        "tasks": cap.tasks,
        "timeout_s": min(DEFAULT_TIMEOUT_S, cap.timeout_s),
    }
    values = {}
    for key, default in defaults.items():
        value = raw.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RequestError(f"limits.{key} must be a positive integer")
        values[key] = min(value, getattr(cap, key))  # clamp to the node policy
    return Limits(**values)


def _env(raw: Any, extra: Dict[str, str]) -> Dict[str, str]:
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or len(raw) > _ENV_MAX_KEYS:
        raise RequestError("env must be an object with at most 64 keys")
    env = {"LANG": "C.UTF-8"}
    for key, value in raw.items():
        if not isinstance(key, str) or key.startswith("CLUSTER_"):
            raise RequestError(f"env key {key!r} is reserved")
        if key not in _ENV_FIXED and not _ENV_USER.fullmatch(key):
            raise RequestError(f"env key {key!r} is not allowed")
        env[key] = _clean_str(value, f"env {key}", _ENV_VALUE_MAX)
    env.pop("HOME", None)  # always the run's work directory, set by the runner
    env.update(extra)  # policy-defined env of a root_op wins
    return env


def parse_exec(data: Dict[str, Any], policy: Policy) -> ExecPlan:
    kind = data["kind"]
    run_id = _run_id(data)
    as_root = data.get("as_root", False)
    if not isinstance(as_root, bool):
        raise RequestError("as_root must be a boolean")
    network = data.get("network", "internet")
    if network not in NETWORKS:
        raise RequestError(f"network must be one of {', '.join(NETWORKS)}")
    if network == "lan" and not policy.lan_cidrs:
        raise RequestError("network=lan needs lan_cidrs in the node policy")
    limits = _limits(data.get("limits"), policy.limits_max)

    if kind == "root_op":
        op_id = data.get("root_op")
        op = policy.root_ops.get(op_id) if isinstance(op_id, str) else None
        if op is None:
            raise RequestError(f"root_op {op_id!r} is not in the node policy")
        if op.survive_disconnect:
            raise RequestError(f"root_op {op_id} needs survive_disconnect (not supported yet)")
        # argv, env and execution mode come from the node policy; any argv sent is ignored.
        return ExecPlan(
            run_id,
            kind,
            op.render(data.get("params")),
            True,
            limits,
            network,
            _env(data.get("env"), op.env),
            detach=op.detach,
            root_op=op.id,
        )

    mode = data.get("mode")
    if mode == "preset":
        if as_root:
            raise RequestError("root presets must use kind=root_op")
        argv = data.get("argv")
        if not isinstance(argv, list) or not argv:
            raise RequestError("argv must be a non-empty list")
        argv = [_clean_str(a, "argv element") for a in argv]
        if sum(len(a) for a in argv) > _ARG_MAX:
            raise RequestError("argv is too long")
    elif mode == "shell":
        if not policy.allow_shell:
            raise RequestError("shell commands are disabled by the node policy")
        if as_root and not policy.allow_as_root_shell:
            raise RequestError("as_root shell is disabled by the node policy")
        # /bin/sh -c receives the whole command as one argv element.
        argv = [SHELL, "-c", _clean_str(data.get("command"), "command", allow_newline=True)]
    else:
        raise RequestError("mode must be preset or shell")
    return ExecPlan(run_id, kind, argv, as_root, limits, network, _env(data.get("env"), {}))


def parse_collect(data: Dict[str, Any]) -> CollectPlan:
    run_id = _run_id(data)
    patterns = data.get("paths")
    if not isinstance(patterns, list) or not patterns or len(patterns) > 32:
        raise RequestError("paths must be a list of 1-32 patterns")
    for p in patterns:
        if (
            not isinstance(p, str)
            or not _COLLECT_PATTERN.fullmatch(p)
            or p.startswith("/")
            or ".." in p.split("/")
        ):
            raise RequestError(f"invalid collect pattern {p!r}")
    plan = CollectPlan(run_id, list(patterns))
    for key in ("max_files", "max_total_bytes", "max_file_bytes"):
        if key in data:
            value = data[key]
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise RequestError(f"{key} must be a positive integer")
            setattr(plan, key, min(value, getattr(plan, key)))
    return plan


def parse_request(line: bytes, policy: Policy) -> Any:
    data = decode_request(line)
    kind = data.get("kind")
    if kind in EXEC_KINDS:
        return parse_exec(data, policy)
    if kind == "collect":
        return parse_collect(data)
    if kind in ("stop", "cleanup"):
        return SimplePlan(kind, _run_id(data))
    if kind == "info":
        return SimplePlan(kind)
    if kind in NOT_YET:
        raise RequestError(f"kind {kind} is not supported by this execd version")
    raise RequestError(f"unknown kind {kind!r}")
