import json

from conftest import TEST_POLICY

from cluster_execd.policy import parse_policy
from cluster_execd.request import parse_request
from cluster_execd.units import Paths, run_argv, stop_argv

PATHS = Paths()


def plan_for(**fields):
    base = {"v": 1, "run_id": "r_42", "kind": "command", "mode": "shell", "command": "df -h"}
    base.update(fields)
    policy = parse_policy(dict(TEST_POLICY, allow_as_root_shell=True))
    return parse_request(json.dumps(base).encode(), policy)


def props(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]


def test_sandboxed_command_unit():
    argv = run_argv(plan_for(limits={"memory_mb": 128, "timeout_s": 30}), PATHS, "cluster-run", [])
    assert argv[:2] == ["/usr/bin/systemd-run", "--unit=cluster-run-r_42.service"]
    assert "--slice=cluster-cmd.slice" in argv
    assert "--pipe" in argv and "--wait" in argv
    assert "--collect" not in argv  # failed units stay loaded for Result, then reset-failed
    p = props(argv)
    for expected in [
        "User=cluster-run",
        "Group=cluster-run",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "TemporaryFileSystem=/var/lib/cluster-run/work",
        "BindPaths=/var/lib/cluster-run/work/r_42",
        "WorkingDirectory=/var/lib/cluster-run/work/r_42",
        "InaccessiblePaths=-/etc/cluster-agent -/var/lib/cluster-agent -/etc/cluster-execd "
        "-/run/cluster-execd.sock",
        "MemoryMax=128M",
        "MemorySwapMax=0",
        "RuntimeMaxSec=30",
        "KillMode=control-group",
        "OOMScoreAdjust=500",
    ]:
        assert expected in p, expected
    # the command is passed after "--" as /bin/sh -c <one element>
    assert argv[argv.index("--") :] == ["--", "/bin/sh", "-c", "df -h"]
    assert "--setenv=HOME=/var/lib/cluster-run/work/r_42" in argv


def test_as_root_shell_has_no_sandbox_user():
    argv = run_argv(plan_for(as_root=True), PATHS, "cluster-run", [])
    p = props(argv)
    assert not any(x.startswith("User=") for x in p)
    assert "WorkingDirectory=/" in p
    assert "TasksMax=64" in p and "RuntimeMaxSec=60" in p


def test_detached_root_op_is_scheduled_not_waited():
    argv = run_argv(plan_for(kind="root_op", root_op="system.reboot"), PATHS, "cluster-run", [])
    assert "--on-active=3s" in argv and "--no-block" in argv and "--collect" in argv
    assert "--pipe" not in argv and "--wait" not in argv
    assert argv[-3:] == ["--", "/usr/bin/systemctl", "reboot"]


def test_network_properties():
    none = props(run_argv(plan_for(network="none"), PATHS, "cluster-run", []))
    assert "PrivateNetwork=yes" in none
    lan = props(run_argv(plan_for(network="lan"), PATHS, "cluster-run", ["192.168.1.0/24"]))
    assert "IPAddressDeny=any" in lan
    assert "IPAddressAllow=localhost 192.168.1.0/24" in lan
    internet = props(run_argv(plan_for(), PATHS, "cluster-run", []))
    assert not any(x.startswith(("PrivateNetwork", "IPAddress")) for x in internet)


def test_env_is_sorted_and_explicit():
    argv = run_argv(plan_for(env={"CW_B": "2", "CW_A": "1"}), PATHS, "cluster-run", [])
    envs = [a for a in argv if a.startswith("--setenv=")]
    assert envs == sorted(envs)
    assert "--setenv=CW_A=1" in envs


def test_stop_covers_service_and_timer():
    assert stop_argv("r_42", PATHS) == [
        "/usr/bin/systemctl",
        "stop",
        "--no-block",
        "cluster-run-r_42.service",
        "cluster-run-r_42.timer",
    ]
