from __future__ import annotations

import json

import pytest

from cluster_master.models import (
    CmdResult,
    ExecSpec,
    Metrics,
    ProtocolError,
    json_size,
    parse_agent_message,
    parse_hello,
)

from .conftest import hello, metrics


def _dump(obj) -> str:
    return json.dumps(obj)


def test_hello_parses_and_pins_id():
    h = parse_hello(_dump(hello("rpi3-01", running_commands=["abc"], pending_results=["x_1"])))
    assert h.node_id == "rpi3-01"
    assert h.running_commands == ["abc"]
    assert h.static_info["hostname"] == "rpi3-01"


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "metrics"},  # wrong type first
        dict(hello("Rpi3-01")),  # uppercase node id
        dict(hello("rpi3-01"), extra_field=1),  # unknown field
        dict(hello("rpi3-01"), running_commands=["bad id with spaces"]),
        dict(hello("rpi3-01"), static_info={"nan": float("nan")}),
        dict(hello("rpi3-01"), board=""),
    ],
)
def test_bad_hello(bad):
    with pytest.raises(ProtocolError):
        parse_hello(_dump(bad))


def test_hello_must_be_object_and_text():
    with pytest.raises(ProtocolError) as exc:
        parse_hello("[1,2]")
    assert exc.value.close_code == 1008
    with pytest.raises(ProtocolError) as exc:
        parse_hello("{not json")
    assert exc.value.close_code == 1007
    with pytest.raises(ProtocolError) as exc:
        parse_hello(b"\x00\x01")
    assert exc.value.close_code == 1003


def test_metrics_summary_accessors():
    m = parse_agent_message(_dump(metrics(cpu=42.0)))
    assert isinstance(m, Metrics)
    assert m.cpu_percent() == 42.0
    assert m.mem_percent() == 50.0
    assert m.temp_c() == 45.5
    assert m.disk_percent() == 30.0
    assert m.sched.free_slots == 2
    assert m.extra()["throttled"] == "0x0"


def test_metrics_tolerates_missing_values():
    msg = metrics()
    msg["data"] = {"cpu": {}, "mem": None, "disk": "nope", "temp_c": "hot"}
    m = parse_agent_message(_dump(msg))
    assert m.cpu_percent() is None
    assert m.mem_percent() is None
    assert m.disk_percent() is None
    assert m.temp_c() is None
    assert m.extra() == {}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(ts=-1),
        lambda m: m.update(ts=True),
        lambda m: m.update(ts="now"),
        lambda m: m["data"].update(extra="not an object"),
        lambda m: m["data"].update(cpu={"percent": float("inf")}),
        lambda m: m["sched"].update(free_slots=-1),
        lambda m: m["sched"].update(bogus=1),
        lambda m: m.update(unknown=1),
    ],
)
def test_bad_metrics(mutate):
    msg = metrics()
    mutate(msg)
    with pytest.raises(ProtocolError):
        parse_agent_message(_dump(msg))


def test_cmd_messages():
    out = parse_agent_message(
        _dump({"type": "cmd_output", "run_id": "r1", "stream": "stdout", "data": "x"})
    )
    assert out.stream == "stdout"
    res = parse_agent_message(
        _dump(
            {"type": "cmd_result", "run_id": "r1", "status": "ok", "exit_code": 0, "duration_ms": 5}
        )
    )
    assert isinstance(res, CmdResult) and res.status == "ok"
    with pytest.raises(ProtocolError):
        parse_agent_message(_dump({"type": "cmd_result", "run_id": "r1", "status": "maybe"}))
    with pytest.raises(ProtocolError):
        parse_agent_message(
            _dump({"type": "cmd_output", "run_id": "r1", "stream": "stdin", "data": ""})
        )
    with pytest.raises(ProtocolError, match="unknown message type"):
        parse_agent_message(_dump({"type": "exec", "run_id": "r1"}))


def test_exec_spec_matches_agent_contract():
    spec = ExecSpec(run_id="abc-123", command="echo hi", timeout=30)
    msg = spec.to_message()
    assert msg == {
        "type": "exec",
        "run_id": "abc-123",
        "mode": "shell",
        "command": "echo hi",
        "as_root": False,
        "timeout": 30,
        "network": "internet",
    }
    with pytest.raises(ValueError):
        ExecSpec(run_id="bad id", command="x")
    with pytest.raises(ValueError):
        ExecSpec(run_id="ok", command="x", timeout=0)


def test_json_size_counts_utf8_bytes():
    assert json_size({"a": "é"}) == len('{"a":"é"}'.encode())


def test_lone_surrogates_are_replaced_everywhere():
    raw = (
        '{"type":"metrics","ts":1.0,"data":{"cpu":{"percent":1.0},"na\\udc80me":"a\\udc80b",'
        '"extra":{"throttled":"\\ud800"}}}'
    )
    m = parse_agent_message(raw)
    for key, value in m.data.items():
        key.encode("utf-8")
        json.dumps(value).encode("utf-8")
    assert m.data["na\ufffdme"] == "a\ufffdb"
    assert m.extra()["throttled"] == "\ufffd"
    h = parse_hello(
        json.dumps(hello("rpi3-01", static_info={"h": "x"})).replace('"x"', '"\\udc80"')
    )
    assert json_size(h.static_info) > 0


def test_hostile_shapes_are_protocol_errors():
    with pytest.raises(ProtocolError):
        parse_agent_message('{"type":"metrics","ts":1,"data":' + "[" * 200 + "]" * 200 + "}")
    with pytest.raises(ProtocolError):
        parse_agent_message('{"type":"metrics","ts":1e400,"data":{}}')
    with pytest.raises(ProtocolError):
        parse_agent_message('{"type":"metrics","ts":1,"data":{"a":1e999}}')
    with pytest.raises(ProtocolError):
        parse_agent_message('{"type":"cmd_result","run_id":"r","status":"ok","exit_code":1e30}')
    with pytest.raises(ProtocolError):
        parse_hello(json.dumps(hello("rpi3-01", board="RPI 3")))
    with pytest.raises(ProtocolError):
        parse_hello(json.dumps(hello("rpi3-01", agent_version="1.0\nfake log line")))


def test_long_reason_is_truncated_not_refused():
    res = parse_agent_message(
        json.dumps({"type": "cmd_result", "run_id": "r1", "status": "error", "reason": "x" * 5000})
    )
    assert isinstance(res, CmdResult) and len(res.reason) == 512 and res.reason.endswith("...")


def test_exec_spec_validates_limits_and_env():
    spec = ExecSpec(
        run_id="r", command="x", limits={"memory_mb": 256, "cpu_pct": 50}, env={"CW_A": "1"}
    )
    assert spec.to_message()["limits"] == {"memory_mb": 256, "cpu_pct": 50}
    with pytest.raises(ValueError, match="unknown limits key"):
        ExecSpec(run_id="r", command="x", limits={"mem_mb": 256})
    with pytest.raises(ValueError, match="CW_"):
        ExecSpec(run_id="r", command="x", env={"PATH": "/evil"})
    with pytest.raises(ValueError):
        ExecSpec(run_id="r", command="x", root_op="Reboot Now")


def test_extra_allow_list_matches_the_agent():
    """security.md 8.3: the agent and master constants must be identical."""
    from cluster_agent import agent as agent_mod

    from cluster_master.models import BOARDS, EXTRA_KEYS

    assert "odroidn2" in BOARDS
    assert set(agent_mod.EXTRA_KEYS) == EXTRA_KEYS
    assert len(agent_mod.EXTRA_KEYS) == len(set(agent_mod.EXTRA_KEYS))
    assert {
        "ddr_temp_c",
        "cpu_freq_mhz",
        "thermal_throttle",
        "freq_capped",
        "emmc_life",
    } <= EXTRA_KEYS
