"""Scheduler labels and capacity a node advertises (docs/design/topology.md section 1.2).

Auto-detected values are the defaults; values in config.yaml always win.
"""

from __future__ import annotations

import os
import platform
from typing import Any, Dict, Optional

from .collectors.base import SysFS

_MB = 1024 * 1024


def _default_capacity(board: str, role: str, mem_mb: Optional[int]) -> Dict[str, int]:
    big = mem_mb is not None and mem_mb >= 3 * 1024  # 4GB board (2GB boards report < 3GB)
    if board == "rdkx3":
        if role == "master":  # keep the master responsive: one job slot only
            return {"slots": 1, "bpu_slots": 1, "job_mem_mb": 1536 if big else 512}
        return {"slots": 3, "bpu_slots": 2, "job_mem_mb": 3072 if big else 1280}
    if board == "rpi3":
        return {"slots": 2, "bpu_slots": 0, "job_mem_mb": 512}
    generic_mem = 256 if mem_mb is None else max(256, (mem_mb - 1024) // 64 * 64)
    return {"slots": 1, "bpu_slots": 0, "job_mem_mb": generic_mem}


def _link_speed(sysfs: SysFS) -> Optional[int]:
    for path in sysfs.glob("/sys/class/net/e*/speed"):  # eth0, end0, enp1s0 ...
        speed = sysfs.read_int(path)
        if speed is not None and speed > 0:  # -1 when the link is down
            return speed
    return None


def build_labels(
    board: str,
    static_info: Dict[str, Any],
    configured: Dict[str, str],
    sysfs: Optional[SysFS] = None,
) -> Dict[str, str]:
    sysfs = sysfs or SysFS()
    mem_total = static_info.get("mem_total")
    labels: Dict[str, str] = {
        "board": board,
        "arch": platform.machine(),
        "bpu": str(static_info.get("bpu_cores") or 0),
        "cpus": str(os.cpu_count() or 1),
        "node_role": "worker",
        "storage": "sd",
    }
    if mem_total:
        labels["mem_mb"] = str(int(mem_total // _MB))
    speed = _link_speed(sysfs)
    if speed:
        labels["net_mbps"] = str(speed)
    labels.update({k: str(v) for k, v in configured.items()})
    return labels


def build_capacity(
    board: str, labels: Dict[str, str], configured: Dict[str, int]
) -> Dict[str, int]:
    mem_mb = int(labels["mem_mb"]) if labels.get("mem_mb", "").isdigit() else None
    capacity = _default_capacity(board, labels.get("node_role", "worker"), mem_mb)
    capacity.update(configured)
    return capacity
