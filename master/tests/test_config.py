from __future__ import annotations

import pytest
import yaml

from cluster_master.config import ConfigError, load_config, parse_config


def test_defaults_are_loopback_only():
    cfg = parse_config(None)
    assert cfg.listeners.web.host == "127.0.0.1"
    assert cfg.listeners.agent.port == 8001
    assert cfg.listeners.admin.mode == 0o600
    assert cfg.db_path == "/var/lib/cluster-master/master.db"
    assert cfg.dev.unauthenticated_admin is False


def test_unknown_key_is_an_error():
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({"listeners": {"web": {"host": "127.0.0.1", "prot": 8000}}})
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({"dev": {"unauthenticated_admin": True, "allow_all": True}})


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.200", "::", "master.cluster.internal"])  # noqa: S104
def test_non_loopback_listener_refused(host):
    with pytest.raises(ConfigError, match="loopback"):
        parse_config({"listeners": {"web": {"host": host}}})
    with pytest.raises(ConfigError, match="loopback"):
        parse_config({"listeners": {"agent": {"host": host}}})


def test_timing_consistency():
    with pytest.raises(ConfigError, match="offline_after_s"):
        parse_config({"agent": {"metrics_interval_s": 10, "offline_after_s": 15}})
    with pytest.raises(ConfigError, match="metrics_interval_s"):
        parse_config({"agent": {"metrics_interval_s": 0}})
    with pytest.raises(ConfigError, match="size limits"):
        parse_config({"agent": {"ws_max_bytes": 1024}})


def test_uds_mode_accepts_octal_string():
    cfg = parse_config({"listeners": {"internal": {"path": "/run/x.sock", "mode": "0660"}}})
    assert cfg.listeners.internal.mode == 0o660
    with pytest.raises(ConfigError, match="absolute"):
        parse_config({"listeners": {"admin": {"path": "relative.sock"}}})


def test_bool_is_not_a_number():
    with pytest.raises(ConfigError, match="wrong type"):
        parse_config({"listeners": {"web": {"port": True}}})


def test_origins_must_be_origins():
    parse_config({"web": {"origins": ["https://master.tailnet.ts.net"]}})
    with pytest.raises(ConfigError, match="origin"):
        parse_config({"web": {"origins": ["https://master.tailnet.ts.net/app"]}})
    with pytest.raises(ConfigError, match="origin"):
        parse_config({"web": {"origins": ["master.tailnet.ts.net"]}})


def test_load_from_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(tmp_path / "data"),
                "log_level": "debug",
                "listeners": {"web": {"port": 8080}},
            }
        )
    )
    cfg = load_config(str(path))
    assert cfg.listeners.web.port == 8080
    assert cfg.db_path == str(tmp_path / "data" / "master.db")
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(str(tmp_path / "missing.yaml"))
    path.write_text("listeners: [not, a, mapping]")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(str(path))
