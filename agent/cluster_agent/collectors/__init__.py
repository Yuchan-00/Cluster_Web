"""Metric collection: board detection and merging of the per-board collectors."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from .. import __version__
from .base import Collector, CommandRunner, SysFS, run_command, safe
from .common import CommonCollector
from .mock import MockCollector
from .rdkx3 import RdkX3Collector
from .rpi import RpiCollector

BOARDS = ("auto", "rpi3", "rdkx3", "generic")


def detect_board(sysfs: SysFS) -> str:
    model = sysfs.read("/proc/device-tree/model") or ""
    if "Raspberry Pi" in model:
        return "rpi3"
    if sysfs.glob("/sys/devices/system/bpu/bpu*"):
        return "rdkx3"
    lowered = model.lower()
    if any(k in lowered for k in ("horizon", "rdk", "sunrise", "d-robotics")):
        return "rdkx3"
    return "generic"


class MetricsCollector:
    """Runs the collectors for one node and merges their output into one sample.

    Board collectors may override "temp_c" (their sensor is more accurate than the generic
    thermal zone) and contribute board-specific keys under "extra".
    """

    def __init__(
        self,
        board: str = "auto",
        sysfs: Optional[SysFS] = None,
        run_cmd: CommandRunner = run_command,
        slow_every: int = 3,
        mock_name: Optional[str] = None,
    ) -> None:
        sysfs = sysfs or SysFS()
        if mock_name is not None:
            self.board = board if board in ("rpi3", "rdkx3") else "rpi3"
            self.collectors: List[Collector] = [MockCollector(self.board, mock_name)]
        else:
            self.board = detect_board(sysfs) if board == "auto" else board
            self.collectors = [CommonCollector(sysfs)]
            if self.board == "rpi3":
                self.collectors.append(RpiCollector(sysfs, run_cmd))
            elif self.board == "rdkx3":
                self.collectors.append(RdkX3Collector(sysfs, run_cmd))
        self.slow_every = max(1, slow_every)
        self._count = 0

    def static_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {}
        for c in self.collectors:
            info.update(safe(c.static_info, {}, what=c.name + ".static") or {})
        info["board"] = self.board
        info["agent_version"] = __version__
        return info

    def collect(self) -> Dict[str, Any]:
        """One sample. Slow metrics run first on every slow_every-th call so they are fresh."""
        sample: Dict[str, Any] = {"extra": {}}
        slow = self._count % self.slow_every == 0
        self._count += 1
        for c in self.collectors:
            if slow:
                _merge(sample, safe(c.collect_slow, {}, what=c.name + ".slow") or {})
            _merge(sample, safe(c.collect, {}, what=c.name) or {})
        sample["ts"] = time.time()
        return sample


def _merge(sample: Dict[str, Any], part: Dict[str, Any]) -> None:
    for key, value in part.items():
        if key == "extra":
            sample["extra"].update(value or {})
        elif key == "temp_c" and value is None:
            continue  # a board collector without a reading must not erase the generic one
        else:
            sample[key] = value


__all__ = ["BOARDS", "MetricsCollector", "SysFS", "detect_board"]
