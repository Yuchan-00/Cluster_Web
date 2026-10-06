"""Raspberry Pi specific metrics: under-voltage/throttling flags and core voltage.

Temperature and clock already come from CommonCollector (psutil reads cpu_thermal and
cpufreq), so this collector only spawns vcgencmd for what psutil cannot see.
The agent account must be in the "video" group for vcgencmd to work.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .base import Collector, CommandRunner, SysFS, run_command

# get_throttled bit layout (Raspberry Pi docs): bits 0-3 are "now", bits 16-19 "since boot".
THROTTLE_FLAGS = {0: "under_voltage", 1: "freq_capped", 2: "throttled", 3: "soft_temp_limit"}
_FIRMWARE_THROTTLED = "/sys/devices/platform/soc/soc:firmware/get_throttled"
_VCGENCMD_TIMEOUT = 2.0


def decode_throttled(value: int) -> Dict[str, Any]:
    return {
        "raw": hex(value),
        "now": {name: bool(value & (1 << bit)) for bit, name in THROTTLE_FLAGS.items()},
        "since_boot": {
            name: bool(value & (1 << (bit + 16))) for bit, name in THROTTLE_FLAGS.items()
        },
    }


def parse_throttled(text: Optional[str]) -> Optional[int]:
    """'throttled=0x50005' -> 0x50005"""
    m = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", text or "")
    return int(m.group(1), 0) if m else None


def parse_volts(text: Optional[str]) -> Optional[float]:
    """'volt=1.2000V' -> 1.2"""
    m = re.search(r"volt=([\d.]+)V", text or "")
    return float(m.group(1)) if m else None


class RpiCollector(Collector):
    name = "rpi"

    def __init__(self, sysfs: Optional[SysFS] = None, run_cmd: CommandRunner = run_command):
        self.sysfs = sysfs or SysFS()
        self.run_cmd = run_cmd

    def collect(self) -> Dict[str, Any]:
        value = self._throttled()
        return {"extra": {"throttled": decode_throttled(value) if value is not None else None}}

    def collect_slow(self) -> Dict[str, Any]:
        volts = parse_volts(self.run_cmd(["vcgencmd", "measure_volts", "core"], _VCGENCMD_TIMEOUT))
        return {"extra": {"core_volts": volts}}

    def _throttled(self) -> Optional[int]:
        value = parse_throttled(self.run_cmd(["vcgencmd", "get_throttled"], _VCGENCMD_TIMEOUT))
        if value is not None:
            return value
        # Newer kernels expose the same value without needing the video group.
        text = self.sysfs.read(_FIRMWARE_THROTTLED)
        if text:
            try:
                return int(text, 16)
            except ValueError:
                return None
        return None
