#!/bin/sh
# Install cluster-agent + cluster-execd on one node (Phase 1 skeleton; Ansible wraps this later).
#
#   read -rs T && printf %s "$T" | sudo ./install_agent.sh \
#       --node-id rpi3-01 --master wss://master.cluster.internal/ws/agent \
#       --ca ca.pem --token-file - [--board auto|rpi3|rdkx3] [--release DIR]
#
# The token is accepted on stdin only, so it never appears in argv, shell history or `ps`
# (docs/design/security.md 20, Phase 1). --release is an unpacked CI release (checksums
# verified beforehand): wheels/ (cluster_agent, cluster_execd and any pinned dependency wheels
# the distribution lacks), deploy/ and policy.example.yaml. Nothing is fetched from the network.
set -eu

die() { echo "install_agent: $*" >&2; exit 1; }
log() { echo "install_agent: $*"; }

NODE_ID= MASTER= CA= TOKEN_FILE= BOARD=auto RELEASE=$(cd "$(dirname "$0")" && pwd)
while [ $# -gt 0 ]; do
    case $1 in
        --node-id) NODE_ID=$2; shift 2 ;;
        --master) MASTER=$2; shift 2 ;;
        --ca) CA=$2; shift 2 ;;
        --token-file) TOKEN_FILE=$2; shift 2 ;;
        --board) BOARD=$2; shift 2 ;;
        --release) RELEASE=$2; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[ "$(id -u)" = 0 ] || die "run as root"
[ "$TOKEN_FILE" = "-" ] || die "the token is read from stdin only: use --token-file -"
printf %s "$NODE_ID" | grep -Eq '^[a-z0-9][a-z0-9-]{0,62}$' || die "invalid --node-id"
case $MASTER in wss://*) ;; *) die "--master must be a wss:// URL" ;; esac
[ -f "$CA" ] || die "--ca file not found"
case $BOARD in auto|rpi3|rdkx3|generic) ;; *) die "invalid --board" ;; esac
[ -d "$RELEASE/wheels" ] && [ -d "$RELEASE/deploy" ] && [ -f "$RELEASE/policy.example.yaml" ] \
    || die "release directory incomplete (wheels/, deploy/, policy.example.yaml)"
command -v systemd-run >/dev/null || log "WARNING: no systemd-run: execd falls back to mode B"
for tool in setpriv prlimit python3; do
    command -v "$tool" >/dev/null || die "$tool is required"
done

TOKEN=$(head -c 200 | tr -d '\r\n')
printf %s "$TOKEN" | grep -Eq '^cat_[A-Za-z0-9_-]{43}$' || die "stdin does not hold a node token"

# -- accounts: the daemon (holds the token) and the run account (runs user code) share nothing
for user in cluster-agent cluster-run; do
    if ! id "$user" >/dev/null 2>&1; then
        useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
            --user-group "$user"
        log "created account $user"
    fi
    passwd -l "$user" >/dev/null 2>&1 || true
done

# -- code: two venvs owned by root. Dependencies come from signed distribution packages where
# they are new enough (security.md 17), otherwise from pinned wheels shipped in the release.
if command -v apt-get >/dev/null; then
    apt-get install -y --no-install-recommends python3-venv python3-psutil python3-yaml \
        python3-websockets util-linux >/dev/null
fi
install_venv() {  # venv name, distribution name
    python3 -m venv --system-site-packages "/opt/$1/venv"
    "/opt/$1/venv/bin/pip" install --no-index --find-links "$RELEASE/wheels" \
        --disable-pip-version-check --quiet "$2"
}
install_venv cluster-agent cluster-agent
install_venv cluster-execd cluster-execd
chmod -R go-w /opt/cluster-agent /opt/cluster-execd

# -- configuration and secrets
install -d -m 0750 -o root -g cluster-agent /etc/cluster-agent
install -m 0644 -o root -g root "$CA" /etc/cluster-agent/ca.pem
umask 077
printf '%s\n' "$TOKEN" >/etc/cluster-agent/agent.token
chown cluster-agent:cluster-agent /etc/cluster-agent/agent.token
chmod 0600 /etc/cluster-agent/agent.token
unset TOKEN
umask 022
if [ ! -f /etc/cluster-agent/config.yaml ]; then
    cat >/etc/cluster-agent/config.yaml <<CONF
node_id: $NODE_ID
master_url: $MASTER
board: $BOARD
token_file: /etc/cluster-agent/agent.token
ca_file: /etc/cluster-agent/ca.pem
CONF
    chmod 0640 /etc/cluster-agent/config.yaml
    chgrp cluster-agent /etc/cluster-agent/config.yaml
fi

install -d -m 0755 -o root -g root /etc/cluster-execd
if [ ! -f /etc/cluster-execd/policy.yaml ]; then
    # Starts from the safe example (as_root shell off). Review limits_max for this board.
    install -m 0644 -o root -g root "$RELEASE/policy.example.yaml" \
        /etc/cluster-execd/policy.yaml
    log "installed default policy: review /etc/cluster-execd/policy.yaml (limits_max)"
fi
/opt/cluster-execd/venv/bin/cluster-execd --check-policy >/dev/null \
    || die "policy check failed: fix /etc/cluster-execd/policy.yaml"

# -- units
install -m 0644 "$RELEASE"/deploy/systemd/cluster-agent.service \
    "$RELEASE"/deploy/systemd/cluster-execd.socket "$RELEASE"/deploy/systemd/cluster-execd@.service \
    "$RELEASE"/deploy/systemd/cluster-cmd.slice "$RELEASE"/deploy/systemd/cluster-jobs.slice \
    /etc/systemd/system/
install -m 0644 "$RELEASE/deploy/tmpfiles/cluster-execd.conf" /etc/tmpfiles.d/
systemd-tmpfiles --create /etc/tmpfiles.d/cluster-execd.conf
systemctl daemon-reload
systemctl enable --now cluster-execd.socket
systemctl enable --now cluster-agent.service

log "done. Check: systemctl status cluster-agent; ss -ltnp shows no agent listener"
log "then confirm labels and capacity for $NODE_ID in the web UI (security.md 8.3)"
