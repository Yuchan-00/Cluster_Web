"""Board-independent metrics gathered through psutil."""

from __future__ import annotations

import os
import platform
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import psutil

from .base import Collector, SysFS, safe

# Pseudo or read-only filesystems that only add noise to the disk table.
_SKIP_FSTYPES = {"squashfs", "tmpfs", "devtmpfs", "overlay", "ramfs", "iso9660"}
# psutil sensor names, most specific first (Pi: cpu_thermal, many ARM SoCs: soc/cpu thermal zones).
_TEMP_SENSORS = ("cpu_thermal", "cpu-thermal", "soc_thermal", "soc-thermal", "coretemp", "k10temp")
TOP_PROCESSES = 5


class CommonCollector(Collector):
    name = "common"

    def __init__(
        self, sysfs: Optional[SysFS] = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.sysfs = sysfs or SysFS()
        self._clock = clock
        self._prev_net: Optional[Tuple[float, Dict[str, Tuple[int, int]]]] = None
        self._prev_disk: Optional[Tuple[float, int, int]] = None
        # The first cpu_percent(interval=None) call always returns 0.0; prime it now.
        safe(lambda: psutil.cpu_percent(percpu=True), what="cpu prime")

    # -- static -----------------------------------------------------------------

    def static_info(self) -> Dict[str, Any]:
        return {
            "hostname": socket.gethostname(),
            "os": safe(self._os_name, what="os"),
            "kernel": platform.release(),
            "arch": platform.machine(),
            "cpu_model": safe(self._cpu_model, what="cpu_model"),
            "cpu_count": psutil.cpu_count(logical=True),
            "mem_total": safe(lambda: psutil.virtual_memory().total, what="mem_total"),
            "boot_time": safe(psutil.boot_time, what="boot_time"),
            "device_model": self.sysfs.read("/proc/device-tree/model"),
            "interfaces": safe(_interfaces, {}, what="interfaces"),
            "python": platform.python_version(),
        }

    def _os_name(self) -> Optional[str]:
        text = self.sysfs.read("/etc/os-release") or ""
        for line in text.splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
        return None

    def _cpu_model(self) -> Optional[str]:
        fields: Dict[str, str] = {}
        for line in (self.sysfs.read("/proc/cpuinfo") or "").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields.setdefault(key.strip(), value.strip())
        # x86 and some ARM kernels expose "model name"; Raspberry Pi kernels expose "Model".
        for key in ("model name", "Model", "Hardware"):
            if fields.get(key):
                return fields[key]
        return platform.processor() or None

    # -- per interval -----------------------------------------------------------

    def collect(self) -> Dict[str, Any]:
        now = self._clock()
        return {
            "cpu": safe(self._cpu, {}, what="cpu"),
            "mem": safe(_memory, {}, what="mem"),
            "swap": safe(_swap, {}, what="swap"),
            "disk": safe(_disks, [], what="disk"),
            "disk_io": safe(lambda: self._disk_io(now), {}, what="disk_io"),
            "net": safe(lambda: self._net(now), {}, what="net"),
            "temp_c": safe(self._temperature, what="temp"),
            "uptime_s": safe(lambda: int(time.time() - psutil.boot_time()), what="uptime"),
            "procs": safe(lambda: len(psutil.pids()), what="procs"),
        }

    def collect_slow(self) -> Dict[str, Any]:
        return {
            "top": safe(_top_processes, [], what="top"),
            # unattended-upgrades leaves this behind when a reboot is needed (security.md 17)
            "extra": {"reboot_required": self.sysfs.exists("/run/reboot-required")},
        }

    def _cpu(self) -> Dict[str, Any]:
        per_core = psutil.cpu_percent(percpu=True)
        freq = safe(psutil.cpu_freq, what="cpu_freq")  # None on some ARM kernels
        return {
            "percent": round(sum(per_core) / len(per_core), 1) if per_core else None,
            "per_core": per_core,
            "freq_mhz": round(freq.current) if freq and freq.current else None,
            "load": [round(x, 2) for x in os.getloadavg()],
        }

    def _disk_io(self, now: float) -> Dict[str, Any]:
        counters = psutil.disk_io_counters()
        if counters is None:
            return {}
        prev, self._prev_disk = self._prev_disk, (now, counters.read_bytes, counters.write_bytes)
        if prev is None or now <= prev[0]:
            return {}
        dt = now - prev[0]
        return {
            "read_bps": _rate(counters.read_bytes, prev[1], dt),
            "write_bps": _rate(counters.write_bytes, prev[2], dt),
        }

    def _net(self, now: float) -> Dict[str, Any]:
        counters = {
            nic: (c.bytes_recv, c.bytes_sent)
            for nic, c in psutil.net_io_counters(pernic=True).items()
            if nic != "lo"
        }
        prev, self._prev_net = self._prev_net, (now, counters)
        if prev is None or now <= prev[0]:
            return {}
        dt = now - prev[0]
        out = {}
        for nic, (rx, tx) in counters.items():
            if nic in prev[1]:
                prx, ptx = prev[1][nic]
                out[nic] = {"rx_bps": _rate(rx, prx, dt), "tx_bps": _rate(tx, ptx, dt)}
        return out

    def _temperature(self) -> Optional[float]:
        sensors = safe(psutil.sensors_temperatures, {}, what="sensors") or {}
        for name in _TEMP_SENSORS:
            if sensors.get(name):
                return round(sensors[name][0].current, 1)
        for entries in sensors.values():
            if entries:
                return round(entries[0].current, 1)
        milli = self.sysfs.read_int("/sys/class/thermal/thermal_zone0/temp")
        return round(milli / 1000.0, 1) if milli is not None else None


def _rate(current: int, previous: int, dt: float) -> int:
    # Counters reset when an interface goes down or wraps; report 0 rather than a negative rate.
    return max(0, int((current - previous) / dt))


def _memory() -> Dict[str, Any]:
    m = psutil.virtual_memory()
    return {
        "total": m.total,
        "available": m.available,
        "used": m.total - m.available,
        "percent": m.percent,
    }


def _swap() -> Dict[str, Any]:
    s = psutil.swap_memory()
    return {"total": s.total, "used": s.used, "percent": s.percent}


def _disks() -> List[Dict[str, Any]]:
    out, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        if part.fstype in _SKIP_FSTYPES or part.mountpoint.startswith("/snap"):
            continue
        if part.device in seen:
            continue
        seen.add(part.device)
        usage = safe(lambda p=part: psutil.disk_usage(p.mountpoint), what="disk_usage")
        if usage is None:
            continue
        out.append(
            {
                "mount": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "percent": usage.percent,
            }
        )
    return out


def _interfaces() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for nic, addrs in psutil.net_if_addrs().items():
        if nic == "lo":
            continue
        entry: Dict[str, Any] = {"ipv4": [], "mac": None}
        for addr in addrs:
            if addr.family == socket.AF_INET:
                entry["ipv4"].append(addr.address)
            elif addr.family == psutil.AF_LINK:
                entry["mac"] = addr.address
        out[nic] = entry
    return out


def _top_processes(limit: int = TOP_PROCESSES) -> List[Dict[str, Any]]:
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent"]):
        info = p.info
        procs.append(
            {
                "pid": info["pid"],
                "name": info["name"],
                "user": info["username"],
                "cpu": round(info["cpu_percent"] or 0.0, 1),
                "mem": round(info["memory_percent"] or 0.0, 1),
            }
        )
    procs.sort(key=lambda x: (x["cpu"], x["mem"]), reverse=True)
    return procs[:limit]
