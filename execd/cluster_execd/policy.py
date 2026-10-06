"""Node-local execution policy: /etc/cluster-execd/policy.yaml (docs/design/security.md 9.4).

This file is the defence line that survives a compromised master: it can only be changed
over SSH/Ansible, so loading refuses files that someone other than root could have written.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

DEFAULT_PATH = "/etc/cluster-execd/policy.yaml"

_OP_ID = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
# Enum values end up inside argv elements: keep them to plain tokens.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:@+=-]{1,64}$")
_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_POLICY_KEYS = {
    "allow_shell",
    "allow_as_root_shell",
    "root_ops",
    "limits_max",
    "lan_cidrs",
    "isolation",
}
_OP_KEYS = {"argv", "params", "env", "readonly", "detach", "survive_disconnect"}


class PolicyError(ValueError):
    """The policy file itself is invalid (execd refuses to start)."""


class RequestError(ValueError):
    """A request violates the policy or is malformed (execd rejects it)."""


@dataclass(frozen=True)
class Limits:
    memory_mb: int
    cpu_pct: int
    tasks: int
    timeout_s: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any], where: str) -> Limits:
        unknown = set(data) - {"memory_mb", "cpu_pct", "tasks", "timeout_s"}
        if unknown:
            raise PolicyError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
        values = {}
        for key in ("memory_mb", "cpu_pct", "tasks", "timeout_s"):
            value = data.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise PolicyError(f"{where}.{key} must be a positive integer")
            values[key] = value
        return cls(**values)


@dataclass(frozen=True)
class ParamSpec:
    name: str
    enum: Optional[Tuple[str, ...]] = None
    int_range: Optional[Tuple[int, int]] = None

    def check(self, value: Any) -> str:
        """Return the value as the string that goes into argv, or raise RequestError."""
        if self.enum is not None:
            if not isinstance(value, str) or value not in self.enum:
                raise RequestError(f"param {self.name} must be one of {list(self.enum)}")
            return value
        if self.int_range is None:
            raise RequestError(f"param {self.name} has no type")
        if not isinstance(value, int) or isinstance(value, bool):
            raise RequestError(f"param {self.name} must be an integer")
        lo, hi = self.int_range
        if not lo <= value <= hi:
            raise RequestError(f"param {self.name} must be between {lo} and {hi}")
        return str(value)


@dataclass(frozen=True)
class RootOp:
    id: str
    argv: Tuple[str, ...]
    params: Dict[str, ParamSpec] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)
    readonly: bool = False
    detach: bool = False
    survive_disconnect: bool = False

    def render(self, given: Any) -> List[str]:
        """argv with placeholders filled from validated params. Master-sent argv is never used."""
        given = {} if given is None else given
        if not isinstance(given, dict):
            raise RequestError("params must be an object")
        unknown = set(given) - set(self.params)
        if unknown:
            raise RequestError(f"unknown param(s) for {self.id}: {', '.join(sorted(unknown))}")
        values = {}
        for name, spec in self.params.items():
            if name not in given:
                raise RequestError(f"missing param {name} for {self.id}")
            values[name] = spec.check(given[name])
        return [_PLACEHOLDER.sub(lambda m: values[m.group(1)], part) for part in self.argv]


@dataclass(frozen=True)
class Policy:
    allow_shell: bool
    allow_as_root_shell: bool
    root_ops: Dict[str, RootOp]
    limits_max: Limits
    lan_cidrs: Tuple[str, ...] = ()
    isolation: str = "auto"  # auto | systemd | fallback

    def summary(self) -> Dict[str, Any]:
        """What the agent may tell the master (no argv: the master never needs it)."""
        return {
            "allow_shell": self.allow_shell,
            "allow_as_root_shell": self.allow_as_root_shell,
            "root_ops": {
                op.id: {
                    "readonly": op.readonly,
                    "detach": op.detach,
                    "survive_disconnect": op.survive_disconnect,
                    "params": sorted(op.params),
                }
                for op in self.root_ops.values()
            },
            "limits_max": {
                "memory_mb": self.limits_max.memory_mb,
                "cpu_pct": self.limits_max.cpu_pct,
                "tasks": self.limits_max.tasks,
                "timeout_s": self.limits_max.timeout_s,
            },
        }


def _param_spec(op_id: str, name: str, raw: Any) -> ParamSpec:
    where = f"root_ops.{op_id}.params.{name}"
    if not _PARAM.fullmatch(str(name)) or not isinstance(raw, dict) or len(raw) != 1:
        raise PolicyError(f"{where} must be {{enum: [...]}} or {{int: [lo, hi]}}")
    if "enum" in raw:
        values = raw["enum"]
        if not isinstance(values, list) or not values:
            raise PolicyError(f"{where}.enum must be a non-empty list")
        out = []
        for v in values:
            if not isinstance(v, str) or not _SAFE_TOKEN.fullmatch(v):
                raise PolicyError(f"{where}.enum value {v!r} is not a plain token")
            out.append(v)
        return ParamSpec(name, enum=tuple(out))
    if "int" in raw:
        bounds = raw["int"]
        ok = (
            isinstance(bounds, list)
            and len(bounds) == 2
            and all(isinstance(b, int) and not isinstance(b, bool) for b in bounds)
            and bounds[0] <= bounds[1]
        )
        if not ok:
            raise PolicyError(f"{where}.int must be [lo, hi] integers")
        return ParamSpec(name, int_range=(bounds[0], bounds[1]))
    raise PolicyError(f"{where} must be {{enum: [...]}} or {{int: [lo, hi]}}")


def _root_op(op_id: str, raw: Any) -> RootOp:
    where = f"root_ops.{op_id}"
    if not _OP_ID.fullmatch(str(op_id)):
        raise PolicyError(f"{where}: id must look like 'group.name'")
    if not isinstance(raw, dict):
        raise PolicyError(f"{where} must be a mapping")
    unknown = set(raw) - _OP_KEYS
    if unknown:
        raise PolicyError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise PolicyError(f"{where}.argv must be a non-empty list of strings")
    if not os.path.isabs(argv[0]) or "{" in argv[0]:
        raise PolicyError(f"{where}.argv[0] must be an absolute path without placeholders")
    params = {
        name: _param_spec(op_id, name, spec) for name, spec in (raw.get("params") or {}).items()
    }
    used = {m for a in argv for m in _PLACEHOLDER.findall(a)}
    if used - set(params):
        raise PolicyError(f"{where}.argv uses undeclared param(s): {sorted(used - set(params))}")
    if set(params) - used:
        raise PolicyError(f"{where}.params not used in argv: {sorted(set(params) - used)}")
    env = raw.get("env") or {}
    if not isinstance(env, dict) or not all(
        _ENV_KEY.fullmatch(str(k)) and isinstance(v, str) for k, v in env.items()
    ):
        raise PolicyError(f"{where}.env must map NAME to string")
    flags = {}
    for key in ("readonly", "detach", "survive_disconnect"):
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise PolicyError(f"{where}.{key} must be true or false")
        flags[key] = value
    if flags["detach"] and flags["survive_disconnect"]:
        raise PolicyError(f"{where}: detach and survive_disconnect are mutually exclusive")
    if flags["readonly"] and flags["detach"]:
        raise PolicyError(f"{where}: a detached op cannot be readonly")
    return RootOp(op_id, tuple(argv), params, dict(env), **flags)


def parse_policy(data: Any) -> Policy:
    if not isinstance(data, dict):
        raise PolicyError("policy must be a mapping")
    unknown = set(data) - _POLICY_KEYS
    if unknown:
        raise PolicyError(f"unknown key(s) in policy: {', '.join(sorted(unknown))}")
    for key in ("allow_shell", "allow_as_root_shell"):
        if not isinstance(data.get(key), bool):
            raise PolicyError(f"{key} must be set explicitly to true or false")
    if "limits_max" not in data:
        raise PolicyError("limits_max is required")
    ops = data.get("root_ops") or {}
    if not isinstance(ops, dict):
        raise PolicyError("root_ops must be a mapping")
    cidrs = data.get("lan_cidrs") or []
    if not isinstance(cidrs, list) or not all(isinstance(c, str) for c in cidrs):
        raise PolicyError("lan_cidrs must be a list of CIDR strings")
    isolation = data.get("isolation", "auto")
    if isolation not in ("auto", "systemd", "fallback"):
        raise PolicyError("isolation must be auto, systemd or fallback")
    return Policy(
        allow_shell=data["allow_shell"],
        allow_as_root_shell=data["allow_as_root_shell"],
        root_ops={str(k): _root_op(str(k), v) for k, v in ops.items()},
        limits_max=Limits.from_dict(data["limits_max"] or {}, "limits_max"),
        lan_cidrs=tuple(cidrs),
        isolation=isolation,
    )


def load_policy(path: str = DEFAULT_PATH, require_root_owned: bool = True) -> Policy:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise PolicyError(f"cannot open {path}: {exc}") from exc
    with os.fdopen(fd, "r", encoding="utf-8") as f:
        if require_root_owned:
            # A writable parent directory would let someone swap the file between checks.
            for st, what in (
                (os.fstat(f.fileno()), path),
                (os.stat(os.path.dirname(os.path.abspath(path))), "its directory"),
            ):
                if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise PolicyError(f"{what} must be owned by root and not group/world writable")
        try:
            return parse_policy(yaml.safe_load(f))
        except yaml.YAMLError as exc:
            raise PolicyError(f"invalid YAML in {path}: {exc}") from exc
