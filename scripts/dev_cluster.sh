#!/usr/bin/env bash
# Development cluster: one master in --dev mode + six mock agents (2 RDK X3, 3 Pi 3B, 1 ODROID-N2+), all on loopback.
#
#   scripts/dev_cluster.sh            # start (Ctrl-C stops everything)
#   DEV_DIR=/tmp/cw scripts/dev_cluster.sh
#
# Web API:  http://127.0.0.1:8000/api/nodes   (every loopback request is the admin "dev")
# Agents:   ws://127.0.0.1:8001/ws/agent
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEV_DIR="${DEV_DIR:-$ROOT/.dev-cluster}"
WEB_PORT="${WEB_PORT:-8000}"
AGENT_PORT="${AGENT_PORT:-8001}"
NODES=("rdkx3-01:rdkx3" "rdkx3-02:rdkx3" "rpi3-01:rpi3" "rpi3-02:rpi3" "rpi3-03:rpi3" "odroidn2-01:odroidn2")

command -v uv >/dev/null || { echo "uv is required (https://docs.astral.sh/uv/)"; exit 1; }
mkdir -p "$DEV_DIR/tokens"
chmod 700 "$DEV_DIR"

master() { (cd "$ROOT/master" && uv run --quiet --python 3.10 --extra dev -- "$@"); }
agent()  { (cd "$ROOT/agent"  && uv run --quiet --extra dev -- "$@"); }

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo; echo "stopping..."
  for pid in "${pids[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

# Register nodes while the master is down (direct DB), writing each token to a 0600 file.
for entry in "${NODES[@]}"; do
  name="${entry%%:*}"; board="${entry##*:}"
  token_file="$DEV_DIR/tokens/$name"
  if [ ! -f "$token_file" ]; then
    master cluster-master-admin --dev "$DEV_DIR" node register "$name" --board "$board" \
      --token-file "$token_file" >/dev/null
    echo "registered $name ($board)"
  fi
done

master cluster-master --dev "$DEV_DIR" --dev-web-port "$WEB_PORT" --dev-agent-port "$AGENT_PORT" &
pids+=($!)

# wait for the web listener
for _ in $(seq 1 50); do
  if curl -fs "http://127.0.0.1:$WEB_PORT/api/cluster/summary" >/dev/null 2>&1; then break; fi
  sleep 0.2
done

for entry in "${NODES[@]}"; do
  name="${entry%%:*}"; board="${entry##*:}"
  agent cluster-agent --mock "$board" --name "$name" \
    --master "ws://127.0.0.1:$AGENT_PORT/ws/agent" --token-file "$DEV_DIR/tokens/$name" &
  pids+=($!)
done

echo
echo "master:  http://127.0.0.1:$WEB_PORT/api/nodes"
echo "admin:   (cd master && uv run cluster-master-admin --dev $DEV_DIR status)"
echo "Ctrl-C to stop."
wait
