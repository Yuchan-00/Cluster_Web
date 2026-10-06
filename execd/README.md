# cluster-execd

Root execution broker for Cluster Web. `cluster-agent` runs unprivileged and asks execd over
`/run/cluster-execd.sock` to run commands. execd checks the request against the node-local
policy `/etc/cluster-execd/policy.yaml`, then starts it as a sandboxed transient systemd unit
running as `cluster-run`. The policy can only be changed over SSH/Ansible, so it stays a
defence line even when the master is compromised. This replaces sudoers. Design:
[docs/design/security.md](../docs/design/security.md) 9–11.

| Piece | File |
|---|---|
| Policy loading and validation (root-owned file, fixed root operations, limits) | `cluster_execd/policy.py` |
| Request validation (run_id, kinds, modes, limits clamp, env allow-list) | `cluster_execd/request.py` |
| systemd-run command lines (sandbox properties of security.md 11.2) | `cluster_execd/units.py` |
| Running: isolation mode A (systemd) and mode B (fallback: setpriv + prlimit) | `cluster_execd/runner.py` |
| Output collection as `cluster-run` (no symlinks, hardlinks, FIFOs, foreign files) | `cluster_execd/collect.py`, `collect_worker.py` |
| Wire protocol (one JSON-line request per connection, JSON-line events back) | `cluster_execd/protocol.py` |

Implemented kinds: `command`, `root_op` (including `detach`), `stop`, `cleanup`, `collect`, `info`.
Not yet: `job`, `install_bundle` (Phase 7) and `survive_disconnect` root ops (Phase 4). Those
requests are rejected.

```bash
sudo cluster-execd --check-policy          # validate /etc/cluster-execd/policy.yaml
uv run --extra dev -- python -m pytest -q  # tests (root/systemd tests skip unless available)
```

Tests marked `root` (uid switch, collect attacks) need euid 0. Tests marked `systemd` need
systemd as PID 1. CI runs both on the runner VM.
