import pytest

from cluster_agent.config import ConfigError, load_config, parse_config
from cluster_agent.labels import build_capacity, build_labels

BASE = {"node_id": "rpi3-01", "master_url": "wss://master.cluster.internal/ws/agent"}


def test_minimal_config_defaults():
    cfg = parse_config(dict(BASE))
    assert cfg.board == "auto"
    assert cfg.metrics_interval == 5.0
    assert cfg.commands.max_concurrent == 2


def test_load_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "node_id: rdkx3-02\n"
        "master_url: wss://master.cluster.internal/ws/agent\n"
        "labels: {node_role: worker, standby: master}\n"
        "capacity: {slots: 3, bpu_slots: 2}\n"
        "commands: {max_concurrent: 3}\n"
    )
    cfg = load_config(str(path))
    assert cfg.labels == {"node_role": "worker", "standby": "master"}
    assert cfg.capacity == {"slots": 3, "bpu_slots": 2}
    assert cfg.commands.max_concurrent == 3


@pytest.mark.parametrize(
    "override, message",
    [
        ({"node_id": "RPI_01"}, "node_id"),
        ({"master_url": "ws://master.cluster.internal/ws/agent"}, "wss://"),
        ({"master_url": "https://master/ws"}, "wss://"),
        ({"board": "rpi4"}, "board"),
        ({"metrics_interval": 0.1}, "metrics_interval"),
        ({"labels": {"Bad Key": "x"}}, "invalid label"),
        ({"labels": {"role": "a b"}}, "invalid label"),
        ({"capacity": {"cores": 2}}, "unknown capacity"),
        ({"capacity": {"slots": -1}}, "non-negative"),
        ({"capacity": {"slots": True}}, "non-negative"),
        ({"tokn_file": "/tmp/x"}, "unknown key"),
        ({"commands": {"max_concurent": 2}}, "unknown key"),
    ],
)
def test_invalid_configs(override, message):
    with pytest.raises(ConfigError, match=message):
        parse_config({**BASE, **override})


def test_missing_required():
    with pytest.raises(ConfigError, match="node_id"):
        parse_config({"master_url": BASE["master_url"]})


def test_insecure_ws_only_for_loopback_in_dev_mode():
    dev = {**BASE, "master_url": "ws://127.0.0.1:8000/ws/agent"}
    with pytest.raises(ConfigError):
        parse_config(dev)
    assert parse_config({**dev, "allow_insecure_loopback": True})
    with pytest.raises(ConfigError):
        parse_config({**BASE, "master_url": "ws://10.0.0.5/ws", "allow_insecure_loopback": True})


def test_unreadable_or_bad_yaml(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(str(tmp_path / "missing.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("node_id: [unclosed\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(str(bad))


def test_labels_detected_then_overridden(make_sysfs):
    sysfs = make_sysfs({"/sys/class/net/eth0/speed": "100\n"})
    info = {"mem_total": 948 * 1024 * 1024, "bpu_cores": None}
    labels = build_labels("rpi3", info, {"storage": "ssd"}, sysfs)
    assert labels["board"] == "rpi3"
    assert labels["bpu"] == "0"
    assert labels["mem_mb"] == "948"
    assert labels["net_mbps"] == "100"
    assert labels["node_role"] == "worker"
    assert labels["storage"] == "ssd"


def test_link_down_speed_ignored(make_sysfs):
    labels = build_labels("generic", {}, {}, make_sysfs({"/sys/class/net/eth0/speed": "-1"}))
    assert "net_mbps" not in labels


@pytest.mark.parametrize(
    "board, role, mem_mb, expected",
    [
        ("rpi3", "worker", 948, {"slots": 2, "bpu_slots": 0, "job_mem_mb": 384}),
        ("rdkx3", "worker", 1900, {"slots": 3, "bpu_slots": 2, "job_mem_mb": 1024}),
        ("rdkx3", "worker", 3800, {"slots": 3, "bpu_slots": 2, "job_mem_mb": 2816}),
        ("rdkx3", "master", 1900, {"slots": 1, "bpu_slots": 1, "job_mem_mb": 384}),
        ("rdkx3", "master", 3800, {"slots": 1, "bpu_slots": 1, "job_mem_mb": 1408}),
        ("odroidn2", "worker", 3800, {"slots": 4, "bpu_slots": 0, "job_mem_mb": 3328}),
        ("odroidn2", "worker", 1900, {"slots": 2, "bpu_slots": 0, "job_mem_mb": 1280}),
        ("odroidn2", "master", 3800, {"slots": 1, "bpu_slots": 0, "job_mem_mb": 2432}),
    ],
)
def test_default_capacity_matches_topology_table(board, role, mem_mb, expected):
    labels = {"node_role": role, "mem_mb": str(mem_mb)}
    assert build_capacity(board, labels, {}) == expected


def test_configured_capacity_wins():
    cap = build_capacity("rpi3", {"mem_mb": "948"}, {"slots": 1})
    assert cap == {"slots": 1, "bpu_slots": 0, "job_mem_mb": 384}


@pytest.mark.parametrize("node_id", ["rpi3-01\n", "rpi3-01\n\n"])
def test_trailing_newline_rejected(node_id):
    with pytest.raises(ConfigError, match="node_id"):
        parse_config({**BASE, "node_id": node_id})


def test_odroid_labels_carry_variant_and_big_cores(make_sysfs):
    info = {
        "arch": "aarch64",
        "cpu_count": 6,
        "mem_total": 4 * 1024**3,
        "variant": "n2plus",
        "big_cores": 4,
    }
    labels = build_labels("odroidn2", info, {}, make_sysfs({}))
    assert labels["board"] == "odroidn2" and labels["variant"] == "n2plus"
    assert labels["big_cores"] == "4" and labels["cpus"] == "6" and labels["bpu"] == "0"
    cap = build_capacity("odroidn2", labels, {})
    assert cap == {"slots": 4, "bpu_slots": 0, "job_mem_mb": 3328}
