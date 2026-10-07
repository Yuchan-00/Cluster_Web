"""Agent protocol messages as strict pydantic models (docs/protocol.md, security.md 8.3).

Everything the agent sends is validated here before any service sees it. Unknown top-level
fields are rejected (a typo in the agent is a bug, not something to guess at); the free-form
blocks (`static_info`, `data`) are size- and shape-limited instead because board collectors
add keys over time. Every parsed object is cleaned first: no NaN/Infinity, no non-string keys,
bounded nesting, and lone UTF-16 surrogates (which `\\udXXX` JSON escapes can smuggle in)
replaced so that everything downstream is valid UTF-8.
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
# What cluster-execd accepts (execd/cluster_execd/request.py); anything else is rejected there.
EXEC_LIMIT_KEYS = frozenset({"memory_mb", "cpu_pct", "tasks", "timeout_s"})
ENV_KEY = re.compile(r"^CW_[A-Z0-9_]{1,60}$")
MAX_DEPTH = 32
REASON_MAX = 512

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


def safe_text(text: str, limit: int = 200) -> str:
    """Agent-controlled text made safe for a log line: one line, printable, bounded."""
    out = "".join(ch if ch.isprintable() else "�" for ch in text)
    return out if len(out) <= limit else out[: limit - 3] + "..."


def _clean_str(s: str) -> str:
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def clean(value: Any, depth: int = 0) -> Any:
    """Reject what JSON cannot carry and repair what Python's json lets through."""
    if depth > MAX_DEPTH:
        raise ValueError("message nested too deeply")
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number")
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("non-string key")
            out[_clean_str(k)] = clean(v, depth + 1)
        return out
    if isinstance(value, list):
        return [clean(v, depth + 1) for v in value]
    return value


Board = Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,32}$")]
AgentVersion = Annotated[str, Field(pattern=r"^[\x21-\x7e]{1,64}$")]


class Hello(_Strict):
    type: Literal["hello"]
    node_id: NodeId
    board: Board
    agent_version: AgentVersion
    static_info: dict[str, Any] = Field(default_factory=dict)
    running_tasks: list[Any] = Field(default_factory=list, max_length=256)
    unacked_results: list[Any] = Field(default_factory=list, max_length=256)
    orphaned: list[Any] = Field(default_factory=list, max_length=256)
    running_commands: list[RunId] = Field(default_factory=list, max_length=256)
    pending_results: list[RunId] = Field(default_factory=list, max_length=256)


class Sched(_Strict):
    """Scheduler view (Phase 7). Stored and shown, not yet acted on; bounds are sanity only."""

    free_slots: int = Field(ge=0, le=1 << 20)
    free_bpu_slots: int = Field(ge=0, le=1 << 20)
    job_mem_free_mb: int = Field(ge=0, le=1 << 31)
    running: list[Any] = Field(default_factory=list, max_length=64)
    cached_bundles: list[Any] = Field(default_factory=list, max_length=256)


class Metrics(_Strict):
    type: Literal["metrics"]
    ts: float | int
    data: dict[str, Any]
    sched: Sched | None = None

    @field_validator("ts")
    @classmethod
    def _check_ts(cls, v: float | int) -> float | int:
        if isinstance(v, bool) or not math.isfinite(v) or v <= 0 or v > 1 << 40:
            raise ValueError("ts must be a positive epoch time")
        return v

    @field_validator("data")
    @classmethod
    def _check_data(cls, v: dict[str, Any]) -> dict[str, Any]:
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
    exit_code: int | None = Field(default=None, ge=-(1 << 31), le=1 << 31)
    duration_ms: int | None = Field(default=None, ge=0, le=1 << 53)
    output_bytes: int | None = Field(default=None, ge=0, le=1 << 53)
    truncated: bool | None = None
    reason: str | None = None
    dropped_bytes: int | None = Field(default=None, ge=0, le=1 << 53)

    @field_validator("reason", mode="before")
    @classmethod
    def _cut_reason(cls, v: Any) -> Any:
        # a long reason is still a result; keep the head rather than refuse the message
        if isinstance(v, str) and len(v) > REASON_MAX:
            return v[: REASON_MAX - 3] + "..."
        return v


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
    return _validate(Hello, obj, "hello")  # type: ignore[return-value]


def parse_agent_message(raw: str | bytes) -> AgentMessage:
    obj = _load(raw)
    kind = obj.get("type")
    model = _BY_TYPE.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise ProtocolError(f"unknown message type {safe_text(repr(kind), 60)}", close_code=1008)
    return _validate(model, obj, kind)  # type: ignore[return-value]


def _validate(model: type[BaseModel], obj: dict[str, Any], kind: str) -> BaseModel:
    try:
        return model.model_validate(obj)
    except ValidationError as exc:
        raise ProtocolError(f"invalid {kind}: {_short(exc)}", close_code=1008) from None
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ProtocolError(f"invalid {kind}: {safe_text(str(exc), 80)}", close_code=1008) from None


def _load(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raise ProtocolError("binary frames are not part of the protocol", close_code=1003)
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        raise ProtocolError("invalid JSON", close_code=1007) from None
    if not isinstance(obj, dict):
        raise ProtocolError("message must be a JSON object", close_code=1008)
    try:
        return clean(obj)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"invalid message: {exc}", close_code=1008) from None


def _short(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "validation failed"
    e = errs[0]
    loc = ".".join(str(p) for p in e.get("loc", ()))
    return safe_text(f"{loc}: {e.get('msg')}", 160)


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


# -- messages to the agent (docs/protocol.md 4) ----------------------------------------------


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
    argv: list[Annotated[str, Field(max_length=4096)]] | None = Field(default=None, max_length=256)
    root_op: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    params: dict[str, Any] | None = None
    as_root: bool = False
    timeout: int = Field(default=600, ge=1, le=24 * 3600)
    limits: dict[str, int] | None = None
    network: Literal["none", "lan", "internet"] = "internet"
    env: dict[str, str] | None = None

    @field_validator("limits")
    @classmethod
    def _check_limits(cls, v: dict[str, int] | None) -> dict[str, int] | None:
        if v is None:
            return None
        bad = sorted(set(v) - EXEC_LIMIT_KEYS)
        if bad:
            raise ValueError(f"unknown limits key(s): {', '.join(bad)}")
        for key, value in v.items():
            if isinstance(value, bool) or not 0 <= value <= 1 << 31:
                raise ValueError(f"limits.{key} out of range")
        return v

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        if len(v) > 32:
            raise ValueError("too many env entries")
        for key, value in v.items():
            if not ENV_KEY.fullmatch(key):
                raise ValueError(f"env key {key!r} must match CW_[A-Z0-9_]+")
            if len(value) > 4096:
                raise ValueError(f"env value for {key} too long")
        return v

    def to_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"type": "exec"}
        msg.update(self.model_dump(exclude_none=True))
        return msg


__all__ = [
    "BOARDS",
    "EXEC_LIMIT_KEYS",
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
    "clean",
    "json_size",
    "parse_agent_message",
    "parse_hello",
    "safe_text",
    "welcome",
]
