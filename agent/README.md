# cluster-agent

Node agent for Cluster Web. Runs on every node (RDK X3 and Raspberry Pi 3B), collects metrics
and executes commands and jobs for the master. Design: [docs/PLAN.md](../docs/PLAN.md).

Status: work in progress (Phase 1). Metric collectors, the command executor and config loading
are implemented; the master connection follows the security design in
[docs/design/security.md](../docs/design/security.md).

## Development

```bash
cd agent
uv run --extra dev -- python -m pytest -q          # tests (also run in CI on 3.8, 3.11, 3.13)
uvx ruff check . && uvx ruff format --check .        # lint
```

The agent must stay compatible with Python 3.8 (RDK X3 images based on Ubuntu 20.04).
