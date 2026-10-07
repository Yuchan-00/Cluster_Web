"""Agent protocol messages as strict pydantic models (docs/PLAN.md 12.1, security.md 8.3).

Everything the agent sends is validated here before any service sees it. Unknown top-level
fields are rejected (a typo in the agent is a bug, not something to guess at); the free-form
blocks (`static_info`, `data`) are size- and shape-limited instead because board collectors
add keys over time.
"""

from __future__ import annotations

import json
import math
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

NODE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
BOARDS = ("rpi3", "rdkx3", "generic")
# Keys an agent may report under metrics.data.extra (security.md 8.3). Anything else is dropped.
EXTRA_KEYS = frozenset({"bpu", "throttled", "core_volts", "reboot_required", "isolation_mode"})
RESULT_STATUSES = (
    "ok",
    "error",
    "timeout",
    "cancelled",
    "oom",
    "scheduled",
    "rejected",
    "failed_to_start",
)

NodeId = Annotated[str, Field(pattern=NODE_ID.pattern)]
RunId = Annotated[str, Field(pattern=RUN_ID.pattern)]


class ProtocolError(ValueError):
    """A message that must not be processed. `close_code` says how the hub should react."""

    def __init__(self, message: str, close_code: int = 1008) -> None:
        super().__init__(message)
        self.close_code = close_code


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def json_size(value: Any) -> int:
    """Serialized size used for every byte limit (what actually went over the wire)."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _finite_numbers(value: Any, where: str) -> None:
    """NaN/Infinity are valid for Python's json but not for JSON; refuse them everywhere."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{where}: non-finite number")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{where}: non-string key")
            _finite_numbers(v, where)
    elif isinstance(value, list):
        for v in value:
            _finite_numbers(v, where)


class Hello(_Strict):
    type: Literal["hello"]
    node_id: NodeId
    board: str = Field(min_length=1, max_length=32)
    agent_version: str = Field(min_length=1, max_length=64)
    static_info: dict[str, Any] = Field(default_factory=dict)
    running_tasks: list[Any] = Field(default_factory=list, max_length=256)
    unacked_results: list[Any] = Field(default_factory=list, max_length=256)
    orphaned: list[Any] = Field(default_factory=list, max_length=256)
    running_commands: list[RunId] = Field(default_factory=list, max_length=256)
    pending_results: list[RunId] = Field(default_factory=list, max_length=256)

    @field_validator("static_info")
    @classmethod
    def _check_static(cls, v: dict[str, Any]) -> dict[str, Any]:
        _finite_numbers(v, "static_info")
        return v


class Sched(_Strict):
    free_slots: int = Field(ge=0, le=64)
    free_bpu_slots: int = Field(ge=0, le=64)
    job_mem_free_mb: int = Field(ge=0, le=1 << 20)
    running: list[Any] = Field(default_factory=list, max_length=256)
    cached_bundles: list[Any] = Field(default_factory=list, max_length=1024)


class Metrics(_Strict):
    type: Literal["metrics"]
    ts: float | int
    data: dict[str, Any]
    sched: Sched | None = None

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, v: float | int) -> float | int:
        if isinstance(v, bool) or not math.isfinite(v) or v <= 0:
            raise ValueError("ts must be a positive epoch time")
        return v

    @field_validator("data")
    @classmethod
    def _check_data(cls, v: dict[str, Any]) -> dict[str, Any]:
        _finite_numbers(v, "data")
        extra = v.get("extra")
        if extra is not None and not isinstance(extra, dict):
            raise ValueError("data.extra must be an object")
        return v

    # Values the dashboard and alerts read; absent/odd values become None instead of errors.
    def cpu_percent(self) -> float | None:
        return _num(_get(self.data, "cpu", "percent"))

    def mem_percent(self) -> float | None:
        return _num(_get(self.data, "mem", "percent"))

    def temp_c(self) -> float | None:
        return _num(self.data.get("temp_c"))

    def disk_percent(self) -> float | None:
        disks = self.data.get("disk")
        if isinstance(disks, list):
            for d in disks:
                if isinstance(d, dict) and d.get("mount") == "/":
                    return _num(d.get("percent"))
        return None

    def extra(self) -> dict[str, Any]:
        extra = self.data.get("extra")
        return extra if isinstance(extra, dict) else {}


class CmdOutput(_Strict):
    type: Literal["cmd_output"]
    run_id: RunId
    stream: Literal["stdout", "stderr"]
    data: str


class CmdResult(_Strict):
    type: Literal["cmd_result"]
    run_id: RunId
    status: Literal[RESULT_STATUSES]  # type: ignore[valid-type]
    exit_code: int | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    output_bytes: int | None = Field(default=None, ge=0)
    truncated: bool | None = None
    reason: str | None = Field(default=None, max_length=512)
    dropped_bytes: int | None = Field(default=None, ge=0)


class Pong(_Strict):
    type: Literal["pong"]


AgentMessage = Metrics | CmdOutput | CmdResult | Pong
_BY_TYPE: dict[str, type[BaseModel]] = {
    "metrics": Metrics,
    "cmd_output": CmdOutput,
    "cmd_result": CmdResult,
    "pong": Pong,
}


def parse_hello(raw: str | bytes) -> Hello:
    obj = _load(raw)
    if obj.get("type") != "hello":
        raise ProtocolError("first message must be hello", close_code=1008)
    try:
        return Hello.model_validate(obj)
    except ValidationError as exc:
        raise ProtocolError(f"invalid hello: {_short(exc)}", close_code=1008) from None


def parse_agent_message(raw: str | bytes) -> AgentMessage:
    obj = _load(raw)
    kind = obj.get("type")
    model = _BY_TYPE.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise ProtocolError(f"unknown message type {kind!r}", close_code=1008)
    try:
        return model.model_validate(obj)  # type: ignore[return-value]
    except ValidationError as exc:
        raise ProtocolError(f"invalid {kind}: {_short(exc)}", close_code=1008) from None


def _load(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raise ProtocolError("binary frames are not part of the protocol", close_code=1003)
    try:
        obj = json.loads(raw)
    except ValueError:
        raise ProtocolError("invalid JSON", close_code=1007) from None
    if not isinstance(obj, dict):
        raise ProtocolError("message must be a JSON object", close_code=1008)
    return obj


def _short(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "validation failed"
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ()))
    return f"{loc}: {e.get('msg')}"


def _get(d: dict[str, Any], *path: str) -> Any:
    cur: Any = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return float(v)


# -- messages to the agent (PLAN.md 12.1) --------------------------------------------------


def welcome(
    *,
    metrics_interval: float,
    lockdown: bool,
    lease_ttl_s: float,
    work_request_interval_s: float,
    server_time: float,
) -> dict[str, Any]:
    return {
        "type": "welcome",
        "metrics_interval": metrics_interval,
        "server_time": server_time,
        "lockdown": lockdown,
        "lease_ttl_s": lease_ttl_s,
        "work_request_interval_s": work_request_interval_s,
    }


class ExecSpec(_Strict):
    """What the master sends as `exec` (the agent's ExecRequest.from_message reads it)."""

    run_id: RunId
    mode: Literal["shell", "preset"] = "shell"
    command: str | None = Field(default=None, max_length=64 * 1024)
    argv: list[str] | None = Field(default=None, max_length=256)
    root_op: str | None = Field(default=None, max_length=64)
    params: dict[str, Any] | None = None
    as_root: bool = False
    timeout: int = Field(default=600, ge=1, le=24 * 3600)
    limits: dict[str, Any] | None = None
    network: Literal["none", "lan", "internet"] = "internet"
    env: dict[str, str] | None = None

    def to_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"type": "exec"}
        msg.update(self.model_dump(exclude_none=True))
        return msg


__all__ = [
    "BOARDS",
    "EXTRA_KEYS",
    "NODE_ID",
    "RUN_ID",
    "RESULT_STATUSES",
    "AgentMessage",
    "CmdOutput",
    "CmdResult",
    "ExecSpec",
    "Hello",
    "Metrics",
    "Pong",
    "ProtocolError",
    "Sched",
    "json_size",
    "parse_agent_message",
    "parse_hello",
    "welcome",
]
