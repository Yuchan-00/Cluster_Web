"""`cluster-master-admin` in offline mode (no master running) against a --dev data dir."""

from __future__ import annotations

import json
import os
import stat

from cluster_master.cli import main


def _run(capsys, *args: str) -> tuple[int, str]:
    code = main(list(args))
    out = capsys.readouterr().out
    return code, out


def test_register_list_revoke_offline(tmp_path, capsys):
    dev = str(tmp_path / "dev")
    code, out = _run(
        capsys,
        "--dev",
        dev,
        "--json",
        "node",
        "register",
        "rpi3-01",
        "--board",
        "rpi3",
        "--label",
        "zone=a",
        "--slots",
        "2",
    )
    assert code == 0, out
    reg = json.loads(out)
    assert reg["id"] == "rpi3-01" and reg["token"].startswith("cat_")

    code, out = _run(capsys, "--dev", dev, "--json", "node", "list")
    assert code == 0
    (node,) = json.loads(out)
    assert node["labels"] == {"zone": "a"} and node["capacity"] == {"slots": 2}
    assert node["has_token"] and "token" not in node

    code, out = _run(capsys, "--dev", dev, "node", "register", "rpi3-01", "--board", "rpi3")
    assert code == 1  # duplicate

    code, out = _run(capsys, "--dev", dev, "node", "revoke", "rpi3-01")
    assert code == 0 and "revoked" in out
    code, out = _run(capsys, "--dev", dev, "--json", "node", "list")
    assert json.loads(out)[0]["has_token"] is False

    code, out = _run(capsys, "--dev", dev, "--json", "audit", "verify")
    assert code == 0
    result = json.loads(out)
    assert result["ok"] and result["rows"] == 2

    code, out = _run(capsys, "--dev", dev, "audit", "tail")
    assert "node.register" in out and "node.token_revoke" in out
    assert "cat_" not in out


def test_token_file_is_private_and_never_overwritten(tmp_path, capsys):
    dev = str(tmp_path / "dev")
    token_file = tmp_path / "token"
    code, out = _run(
        capsys,
        "--dev",
        dev,
        "node",
        "register",
        "rpi3-02",
        "--board",
        "rpi3",
        "--token-file",
        str(token_file),
    )
    assert code == 0 and "cat_" not in out
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert token_file.read_text().startswith("cat_")
    code, out = _run(
        capsys, "--dev", dev, "node", "rotate", "rpi3-02", "--token-file", str(token_file)
    )
    assert code == 1  # exists: refused


def test_service_token_and_lockdown_offline(tmp_path, capsys):
    dev = str(tmp_path / "dev")
    code, out = _run(
        capsys, "--dev", dev, "--json", "service-token", "create", "telegram-bot", "--scope", "read"
    )
    assert code == 0
    created = json.loads(out)
    assert created["token"].startswith("cst_")
    code, out = _run(capsys, "--dev", dev, "--json", "service-token", "list")
    assert json.loads(out)[0]["scopes"] == ["read"]
    code, out = _run(capsys, "--dev", dev, "service-token", "revoke", str(created["id"]))
    assert code == 0
    code, out = _run(capsys, "--dev", dev, "service-token", "revoke", str(created["id"]))
    assert code == 1

    code, out = _run(capsys, "--dev", dev, "--json", "lockdown", "on", "--reason", "cli test")
    assert code == 0 and json.loads(out)["active"] is True
    code, out = _run(capsys, "--dev", dev, "--json", "status")
    assert json.loads(out)["lockdown"]["reason"] == "cli test"
    code, out = _run(capsys, "--dev", dev, "--json", "lockdown", "off")
    assert json.loads(out)["active"] is False


def test_bad_label_and_missing_config(tmp_path, capsys):
    dev = str(tmp_path / "dev")
    code, _ = _run(
        capsys, "--dev", dev, "node", "register", "rpi3-03", "--board", "rpi3", "--label", "novalue"
    )
    assert code == 1
    code, _ = _run(capsys, "--config", str(tmp_path / "nope.yaml"), "status")
    assert code == 1
