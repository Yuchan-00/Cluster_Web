"""RDK X3 specific metrics: BPU utilisation and SoC temperature.

The sysfs paths follow the RDK X3 documentation; they can differ between OS image
versions, so every source has a fallback and the exact paths are confirmed in Phase 0
(docs/design/topology.md). hrut_somstatus is the vendor tool that prints the same data.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import Collector, CommandRunner, SysFS, run_command

_BPU_RATIO_GLOB = "/sys/devices/system/bpu/bpu*/ratio"
_HWMON_TEMP_GLOB = "/sys/class/hwmon/hwmon*/temp1_input"
_HRUT_TIMEOUT = 3.0

_HRUT_BPU = re.compile(r"^\s*bpu(\d+)\s*:\s*\d+\s+\d+\s+\d+\s+(\d+)\s*$")
_HRUT_TEMP = re.compile(r"^\s*([A-Za-z]+)\s*:\s*(-?[\d.]+)\s*\(C\)")


def parse_hrut_somstatus(text: Optional[str]) -> Dict[str, Any]:
    """Parse the first report block of hrut_somstatus into {"temp_c", "bpu"}.

    The tool may print reports in a loop; only the first block is used.
    """
    temps: Dict[str, float] = {}
    bpu: Dict[int, int] = {}
    blocks_seen = 0
    for line in (text or "").splitlines():
        if line.strip().startswith("====="):
            blocks_seen += 1
            if blocks_seen > 1:
                break
            continue
        m = _HRUT_TEMP.match(line)
        if m:
            temps.setdefault(m.group(1).upper(), float(m.group(2)))
            continue
        m = _HRUT_BPU.match(line)
        if m:
            bpu[int(m.group(1))] = int(m.group(2))
    temp = temps.get("CPU", next(iter(temps.values()), None))
    return {"temp_c": temp, "bpu": [bpu[k] for k in sorted(bpu)] or None}


class RdkX3Collector(Collector):
    name = "rdkx3"
    # Stop spawning hrut_somstatus after this many consecutive failures (tool missing or broken).
    MAX_HRUT_FAILURES = 3

    def __init__(self, sysfs: Optional[SysFS] = None, run_cmd: CommandRunner = run_command):
        self.sysfs = sysfs or SysFS()
        self.run_cmd = run_cmd
        self._fallback: Dict[str, Any] = {"temp_c": None, "bpu": None}
        self._hrut_failures = 0

    def static_info(self) -> Dict[str, Any]:
        cores = len(self.sysfs.glob(_BPU_RATIO_GLOB))
        return {"bpu_cores": cores or None}

    def collect(self) -> Dict[str, Any]:
        bpu = self._bpu_sysfs()
        temp = self._temp_sysfs()
        if bpu is None:
            bpu = self._fallback["bpu"]
        if temp is None:
            temp = self._fallback["temp_c"]
        out: Dict[str, Any] = {"extra": {"bpu": bpu}}
        if temp is not None:
            out["temp_c"] = temp
        return out

    def collect_slow(self) -> Dict[str, Any]:
        # The vendor tool can take seconds (it may loop until killed), so it only runs on
        # slow cycles and only when sysfs lacks a value; collect() reuses the cached result.
        if self._hrut_failures >= self.MAX_HRUT_FAILURES:
            return {}
        if self._bpu_sysfs() is not None and self._temp_sysfs() is not None:
            return {}
        parsed = parse_hrut_somstatus(self.run_cmd(["hrut_somstatus"], _HRUT_TIMEOUT))
        if parsed["bpu"] is None and parsed["temp_c"] is None:
            self._hrut_failures += 1
        else:
            self._hrut_failures = 0
            self._fallback = parsed
        return {}

    def _bpu_sysfs(self) -> Optional[List[int]]:
        paths = self.sysfs.glob(_BPU_RATIO_GLOB)
        if not paths:
            return None
        values = [self.sysfs.read_int(p) for p in paths]
        if any(v is None for v in values):
            return None
        return [int(v) for v in values if v is not None]

    def _temp_sysfs(self) -> Optional[float]:
        for path in self.sysfs.glob(_HWMON_TEMP_GLOB):
            milli = self.sysfs.read_int(path)
            if milli is not None:
                return round(milli / 1000.0, 1)
        return None
