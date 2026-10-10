"""Synthetic metrics so the master and web UI can be developed without hardware."""

from __future__ import annotations

import random
import time
from typing import Any, Dict

from .base import Collector

_PROFILES = {
    "rdkx3": {"cores": 4, "mem": 4 * 1024**3, "model": "RDK X3 (mock)", "arch": "aarch64"},
    "rpi3": {
        "cores": 4,
        "mem": 1024**3,
        "model": "Raspberry Pi 3 Model B (mock)",
        "arch": "aarch64",
    },
    "odroidn2": {
        "cores": 6,
        "mem": 4 * 1024**3,
        "model": "Hardkernel ODROID-N2Plus (mock)",
        "arch": "aarch64",
    },
}


class MockCollector(Collector):
    """Random-walk metrics with a per-node seed, shaped like the real collectors' output."""

    name = "mock"

    def __init__(self, board: str, node_name: str) -> None:
        self.board = board if board in _PROFILES else "rpi3"
        self.node_name = node_name
        self.profile = _PROFILES[self.board]
        self.rng = random.Random(node_name)  # noqa: S311 - not security sensitive
        self.cpu = self.rng.uniform(5, 30)
        self.mem = self.rng.uniform(30, 60)
        self.temp = self.rng.uniform(40, 55)
        self.started = time.time()

    def _walk(self, value: float, step: float, lo: float, hi: float) -> float:
        return min(hi, max(lo, value + self.rng.uniform(-step, step)))

    def static_info(self) -> Dict[str, Any]:
        p = self.profile
        return {
            "hostname": self.node_name,
            "os": "Mock OS",
            "kernel": "mock",
            "arch": p["arch"],
            "cpu_model": p["model"],
            "cpu_count": p["cores"],
            "mem_total": p["mem"],
            "boot_time": self.started,
            "device_model": p["model"],
            "interfaces": {"eth0": {"ipv4": ["192.0.2.10"], "mac": "02:00:00:00:00:01"}},
            "python": "mock",
            "bpu_cores": 2 if self.board == "rdkx3" else None,
            **(
                {"variant": "n2plus", "little_cores": 2, "big_cores": 4, "emmc": True}
                if self.board == "odroidn2"
                else {}
            ),
        }

    def collect(self) -> Dict[str, Any]:
        p = self.profile
        self.cpu = self._walk(self.cpu, 8, 1, 100)
        self.mem = self._walk(self.mem, 3, 10, 95)
        self.temp = self._walk(self.temp, 1.5, 35, 85)
        per_core = [round(self._walk(self.cpu, 10, 0, 100), 1) for _ in range(p["cores"])]
        used = int(p["mem"] * self.mem / 100)
        extra: Dict[str, Any] = {}
        if self.board == "rdkx3":
            extra["bpu"] = [self.rng.randint(0, 60), self.rng.randint(0, 60)]
        elif self.board == "odroidn2":
            extra["ddr_temp_c"] = round(self.temp - 5, 1)
            extra["cpu_freq_mhz"] = {"little": 1896, "big": 2208 if self.cpu > 50 else 1800}
            extra["thermal_throttle"] = False
        else:
            extra["throttled"] = "0x0"
        return {
            "cpu": {
                "percent": round(sum(per_core) / len(per_core), 1),
                "per_core": per_core,
                "freq_mhz": 1200,
                "load": [round(self.cpu / 25, 2)] * 3,
            },
            "mem": {
                "total": p["mem"],
                "available": p["mem"] - used,
                "used": used,
                "percent": round(self.mem, 1),
            },
            "swap": {"total": 0, "used": 0, "percent": 0.0},
            "disk": [
                {
                    "mount": "/",
                    "fstype": "ext4",
                    "total": 32 * 1024**3,
                    "used": 12 * 1024**3,
                    "percent": 37.5,
                }
            ],
            "disk_io": {
                "read_bps": self.rng.randint(0, 50000),
                "write_bps": self.rng.randint(0, 80000),
            },
            "net": {
                "eth0": {
                    "rx_bps": self.rng.randint(0, 200000),
                    "tx_bps": self.rng.randint(0, 100000),
                }
            },
            "temp_c": round(self.temp, 1),
            "uptime_s": int(time.time() - self.started),
            "procs": self.rng.randint(90, 140),
            "extra": extra,
        }

    def collect_slow(self) -> Dict[str, Any]:
        return {"top": [{"pid": 1, "name": "systemd", "user": "root", "cpu": 0.1, "mem": 1.2}]}
