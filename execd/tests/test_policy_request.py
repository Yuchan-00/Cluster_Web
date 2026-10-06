import copy
import json
import os

import pytest
from conftest import TEST_POLICY

from cluster_execd.policy import PolicyError, RequestError, load_policy, parse_policy
from cluster_execd.request import parse_request

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "policy.example.yaml")


def req(**fields):
    base = {"v": 1, "run_id": "r_01", "kind": "command", "mode": "shell", "command": "true"}
    base.update(fields)
    return json.dumps(base).encode()


def mutate(**changes):
    data = copy.deepcopy(TEST_POLICY)
    data.update(changes)
    return data


# -- policy file --------------------------------------------------------------------------


def test_example_policy_is_valid():
    policy = load_policy(EXAMPLE, require_root_owned=False)
    assert policy.allow_as_root_shell is False
    assert policy.root_ops["logs.journal"].readonly is True
    assert "argv" not in json.dumps(policy.summary())  # argv never leaves the node


@pytest.mark.parametrize(
    "data, message",
    [
        (mutate(allow_shell="yes"), "allow_shell"),
        (
            {k: v for k, v in TEST_POLICY.items() if k != "allow_as_root_shell"},
            "allow_as_root_shell",
        ),
        (mutate(extra=1), "unknown key"),
        (
            mutate(limits_max={"memory_mb": 0, "cpu_pct": 1, "tasks": 1, "timeout_s": 1}),
            "memory_mb",
        ),
        (mutate(root_ops={"bad": {"argv": ["/bin/true"]}}), "group.name"),
        (mutate(root_ops={"a.b": {"argv": ["true"]}}), "absolute path"),
        (
            mutate(root_ops={"a.b": {"argv": ["/bin/{x}"], "params": {"x": {"enum": ["a"]}}}}),
            "absolute path",
        ),
        (mutate(root_ops={"a.b": {"argv": ["/bin/echo", "{x}"]}}), "undeclared"),
        (
            mutate(root_ops={"a.b": {"argv": ["/bin/echo"], "params": {"x": {"enum": ["a"]}}}}),
            "not used",
        ),
        (
            mutate(
                root_ops={"a.b": {"argv": ["/bin/echo", "{x}"], "params": {"x": {"enum": ["a b"]}}}}
            ),
            "plain token",
        ),
        (
            mutate(
                root_ops={
                    "a.b": {"argv": ["/bin/echo", "{x}"], "params": {"x": {"enum": ["$(id)"]}}}
                }
            ),
            "plain token",
        ),
        (
            mutate(
                root_ops={"a.b": {"argv": ["/bin/echo", "{x}"], "params": {"x": {"int": [5, 1]}}}}
            ),
            "lo, hi",
        ),
        (
            mutate(
                root_ops={
                    "a.b": {"argv": ["/bin/true"], "detach": True, "survive_disconnect": True}
                }
            ),
            "mutually exclusive",
        ),
        (mutate(root_ops={"a.b": {"argv": ["/bin/true"], "sudo": True}}), "unknown key"),
        (mutate(isolation="none"), "isolation"),
    ],
)
def test_invalid_policies(data, message):
    with pytest.raises(PolicyError, match=message):
        parse_policy(data)


def test_policy_must_not_be_writable_by_others(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(open(EXAMPLE).read())
    os.chmod(path, 0o666)
    with pytest.raises(PolicyError, match="owned by root"):
        load_policy(str(path))


def test_policy_symlink_refused(tmp_path):
    link = tmp_path / "policy.yaml"
    link.symlink_to(os.path.abspath(EXAMPLE))
    with pytest.raises(PolicyError, match="cannot open"):
        load_policy(str(link), require_root_owned=False)


# -- requests ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_id",
    ["", "../etc", "a;b", "a b", "x" * 65, "run\n", "ünicode", 5, None],
)
def test_run_id_injection_rejected(policy, run_id):
    with pytest.raises(RequestError, match="run_id"):
        parse_request(req(run_id=run_id), policy)


def test_shell_command_is_one_argv_element(policy):
    plan = parse_request(req(command="echo a; id"), policy)
    assert plan.argv == ["/bin/sh", "-c", "echo a; id"]
    assert plan.as_root is False and plan.sandboxed


def test_shell_disabled_by_policy(policy):
    data = mutate(allow_shell=False)
    with pytest.raises(RequestError, match="disabled"):
        parse_request(req(), parse_policy(data))


def test_as_root_shell_needs_policy_flag(policy):
    with pytest.raises(RequestError, match="as_root shell is disabled"):
        parse_request(req(as_root=True), policy)
    allowed = parse_policy(mutate(allow_as_root_shell=True))
    plan = parse_request(req(as_root=True), allowed)
    assert plan.as_root and not plan.sandboxed


def test_preset_cannot_be_root(policy):
    with pytest.raises(RequestError, match="kind=root_op"):
        parse_request(req(mode="preset", argv=["id"], as_root=True), policy)


def test_root_op_ignores_master_argv(policy):
    data = req(
        kind="root_op", root_op="diag.echo", params={"word": "hi"}, argv=["/bin/sh", "-c", "evil"]
    )
    plan = parse_request(data, policy)
    assert plan.argv == ["/bin/echo", "hi"]
    assert plan.as_root is True and plan.root_op == "diag.echo"


@pytest.mark.parametrize(
    "params, message",
    [
        ({"word": "$(id)"}, "one of"),
        ({}, "missing"),
        ({"word": "hi", "x": 1}, "unknown"),
        ("hi", "object"),
    ],
)
def test_root_op_params_validated(policy, params, message):
    with pytest.raises(RequestError, match=message):
        parse_request(req(kind="root_op", root_op="diag.echo", params=params), policy)


def test_unknown_or_unsupported_root_ops(policy):
    with pytest.raises(RequestError, match="not in the node policy"):
        parse_request(req(kind="root_op", root_op="system.format"), policy)
    with pytest.raises(RequestError, match="survive_disconnect"):
        parse_request(req(kind="root_op", root_op="apt.update"), policy)


def test_detached_root_op(policy):
    plan = parse_request(req(kind="root_op", root_op="system.reboot"), policy)
    assert plan.detach is True


def test_limits_clamped_and_defaulted(policy):
    plan = parse_request(req(limits={"memory_mb": 99999, "timeout_s": 5}), policy)
    assert plan.limits.memory_mb == 256  # policy cap
    assert plan.limits.timeout_s == 5
    assert plan.limits.tasks == 64
    default = parse_request(req(), policy)
    assert default.limits.timeout_s == 60
    with pytest.raises(RequestError, match="positive integer"):
        parse_request(req(limits={"tasks": -1}), policy)
    with pytest.raises(RequestError, match="unknown limit"):
        parse_request(req(limits={"nice": 1}), policy)


def test_network_modes(policy):
    assert parse_request(req(network="none"), policy).network == "none"
    assert parse_request(req(network="lan"), policy).network == "lan"
    no_lan = parse_policy({k: v for k, v in TEST_POLICY.items() if k != "lan_cidrs"})
    with pytest.raises(RequestError, match="lan_cidrs"):
        parse_request(req(network="lan"), no_lan)
    with pytest.raises(RequestError, match="network"):
        parse_request(req(network="host"), policy)


@pytest.mark.parametrize(
    "env, message",
    [
        ({"CLUSTER_TASK_ID": "x"}, "reserved"),
        ({"PATH": "/tmp"}, "not allowed"),
        ({"LD_PRELOAD": "/tmp/x.so"}, "not allowed"),
        ({"CW_A": "x\ny"}, "control"),
        ({"CW_lower": "x"}, "not allowed"),
    ],
)
def test_env_rules(policy, env, message):
    with pytest.raises(RequestError, match=message):
        parse_request(req(env=env), policy)


def test_env_allowed_and_home_forced(policy):
    plan = parse_request(req(env={"CW_BATCH": "8", "TZ": "Asia/Seoul", "HOME": "/etc"}), policy)
    assert plan.env == {"LANG": "C.UTF-8", "CW_BATCH": "8", "TZ": "Asia/Seoul"}


@pytest.mark.parametrize(
    "line, message",
    [
        (b"not json", "JSON"),
        (b"[1]", "object"),
        (b'{"v": 2}', "version"),
        (b"x" * (300 * 1024), "too large"),
    ],
)
def test_malformed_requests(policy, line, message):
    with pytest.raises(RequestError, match=message):
        parse_request(line, policy)


def test_unknown_and_future_kinds(policy):
    with pytest.raises(RequestError, match="unknown kind"):
        parse_request(req(kind="shell"), policy)
    with pytest.raises(RequestError, match="not supported"):
        parse_request(req(kind="job"), policy)


def test_command_control_characters(policy):
    with pytest.raises(RequestError, match="NUL"):
        parse_request(req(command="echo \x00"), policy)
    plan = parse_request(req(command="echo a\necho b"), policy)  # newlines are fine in shell
    assert plan.argv[2] == "echo a\necho b"
    with pytest.raises(RequestError, match="control"):
        parse_request(req(mode="preset", argv=["echo", "a\x1b[2J"]), policy)


@pytest.mark.parametrize(
    "paths",
    [["/etc/passwd"], ["../x"], ["out/../../x"], [], ["a" * 300], ["out/$(id)"]],
)
def test_collect_patterns_validated(policy, paths):
    with pytest.raises(RequestError):
        parse_request(req(kind="collect", paths=paths), policy)


def test_collect_limits_only_lowered(policy):
    plan = parse_request(req(kind="collect", paths=["out/*"], max_files=10**9), policy)
    assert plan.max_files == 64
