# cluster-agent

Node agent for Cluster Web. It runs on every node (RDK X3 and Raspberry Pi 3B) as the
unprivileged `cluster-agent` account and has no listening port. Its jobs:

- collect metrics,
- keep one TLS WebSocket to the master,
- hand every run to [cluster-execd](../execd/README.md).

Design: [docs/PLAN.md](../docs/PLAN.md), [docs/design/security.md](../docs/design/security.md) 8–9.

| Piece | File |
|---|---|
| Metrics: psutil, Pi throttling, RDK X3 BPU, mock | `cluster_agent/collectors/` |
| Labels and capacity (topology.md 1.2) | `cluster_agent/labels.py` |
| Config with strict validation | `cluster_agent/config.py` |
| TLS pinned to the internal CA, token in the upgrade header, backoff | `cluster_agent/connection.py` |
| Session: hello/welcome, metrics, exec/cancel, lockdown, queued results | `cluster_agent/agent.py` |
| Run bookkeeping, output batching/caps; launchers for execd and dev | `cluster_agent/executor.py`, `execd_client.py` |

## Usage

```bash
cluster-agent --once                       # static info + one metrics sample as JSON
cluster-agent                              # daemon, /etc/cluster-agent/config.yaml
cluster-agent --mock rpi3 --name rpi3-01 \
  --master ws://127.0.0.1:8001/ws/agent --token-file dev.token   # simulated node (dev only)
```

The token file must be mode 0600 and owned by the agent account. Install with
`deploy/install_agent.sh`, which reads the token from stdin only.

## Messages sent to the master (docs/PLAN.md 12.1)

`hello` (static info, labels, capacity, isolation, node policy summary, `running_commands`),
`metrics` every 5 s (doubles as heartbeat), `cmd_output`, and `cmd_result` with `status` set
to one of: ok, error, timeout, cancelled, oom, scheduled, rejected, failed_to_start.

Output is rate limited and dropped bytes are reported. Results produced while disconnected
are sent after the next hello.

## Development

```bash
uv run --extra dev -- python -m pytest -q   # CI: Python 3.8, 3.11, 3.13
uvx ruff check . && uvx ruff format --check .
```

The agent must stay compatible with Python 3.8 (RDK X3 images based on Ubuntu 20.04) and
websockets 10 and later.
