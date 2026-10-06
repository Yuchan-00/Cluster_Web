"""systemd-run command lines for execution plans (docs/design/security.md 11.2).

Pure functions: building the argv is kept separate from running it so the exact unit
properties are unit-tested without systemd.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from .request import ExecPlan

CMD_SLICE = "cluster-cmd.slice"
UNIT_PREFIX = "cluster-run-"
DETACH_DELAY = "3s"


@dataclass(frozen=True)
class Paths:
    """Filesystem layout; overridable so tests can run outside /var/lib."""

    run_root: str = "/var/lib/cluster-run"
    agent_root: str = "/var/lib/cluster-agent"
    agent_etc: str = "/etc/cluster-agent"
    execd_etc: str = "/etc/cluster-execd"
    execd_socket: str = "/run/cluster-execd.sock"
    state_dir: str = "/run/cluster-execd"
    systemd_run: str = "/usr/bin/systemd-run"
    systemctl: str = "/usr/bin/systemctl"

    @property
    def work_root(self) -> str:
        return os.path.join(self.run_root, "work")

    def workdir(self, run_id: str) -> str:
        return os.path.join(self.work_root, run_id)


def unit_name(run_id: str) -> str:
    return f"{UNIT_PREFIX}{run_id}.service"


def _common(plan: ExecPlan) -> List[str]:
    lim = plan.limits
    props = [
        f"MemoryMax={lim.memory_mb}M",
        "MemorySwapMax=0",
        f"CPUQuota={lim.cpu_pct}%",
        f"TasksMax={lim.tasks}",
        f"RuntimeMaxSec={lim.timeout_s}",
        "KillMode=control-group",
        "TimeoutStopSec=5",
        "OOMScoreAdjust=500",
    ]
    return props


def _sandbox(plan: ExecPlan, paths: Paths, run_user: str) -> List[str]:
    workdir = paths.workdir(plan.run_id)
    return [
        f"User={run_user}",
        f"Group={run_user}",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "CapabilityBoundingSet=",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        # Hide other runs' directories, then bring back only this run's one.
        f"TemporaryFileSystem={paths.work_root}",
        f"BindPaths={workdir}",
        f"ReadWritePaths={workdir}",
        f"WorkingDirectory={workdir}",
        # "-": a path that does not exist on this node has nothing to hide (and would otherwise
        # make the unit fail to start).
        "InaccessiblePaths="
        + " ".join(
            "-" + p
            for p in (paths.agent_etc, paths.agent_root, paths.execd_etc, paths.execd_socket)
        ),
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "LockPersonality=yes",
        "RestrictRealtime=yes",
    ]


def _network(plan: ExecPlan, lan_cidrs: List[str]) -> List[str]:
    if plan.network == "none":
        return ["PrivateNetwork=yes"]
    if plan.network == "lan":
        return ["IPAddressDeny=any", "IPAddressAllow=localhost " + " ".join(lan_cidrs)]
    return []


def run_argv(plan: ExecPlan, paths: Paths, run_user: str, lan_cidrs: List[str]) -> List[str]:
    """systemd-run invocation for a command or root_op."""
    props: List[str] = []
    if plan.sandboxed:
        props += _sandbox(plan, paths, run_user)
    else:
        props.append("WorkingDirectory=/")
    props += _common(plan)
    props += _network(plan, lan_cidrs)

    argv = [
        paths.systemd_run,
        f"--unit={unit_name(plan.run_id)}",
        f"--slice={CMD_SLICE}",
        "--quiet",
    ]
    if plan.detach:
        # Disconnecting operations (reboot, agent restart) are only scheduled; the reply goes
        # out before they take effect.
        argv += [f"--on-active={DETACH_DELAY}", "--no-block", "--collect"]
    else:
        # No --collect: a failed unit stays loaded so execd can read Result (oom-kill, timeout,
        # exit code) and then runs reset-failed.
        argv += ["--pipe", "--wait"]
    for prop in props:
        argv += ["-p", prop]
    env = dict(plan.env)
    env["HOME"] = paths.workdir(plan.run_id) if plan.sandboxed else "/root"
    for key in sorted(env):
        argv.append(f"--setenv={key}={env[key]}")
    argv.append("--")
    argv += plan.argv
    return argv


def stop_argv(run_id: str, paths: Paths) -> List[str]:
    # The timer exists only for detached runs; stopping a missing unit is harmless.
    base = f"{UNIT_PREFIX}{run_id}"
    return [paths.systemctl, "stop", "--no-block", f"{base}.service", f"{base}.timer"]
