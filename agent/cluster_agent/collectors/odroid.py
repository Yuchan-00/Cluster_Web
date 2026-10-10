"""ODROID-N2 family (N2 / N2+ / N2L, Amlogic S922X) metrics.

Everything comes from sysfs; there is no vendor tool to spawn. The board has two on-die
temperature sensors (CPU and DDR) and two cpufreq clusters (2x Cortex-A53 little, 4x Cortex-A73
big). Kernel differences that matter (docs/design/topology.md 1.1, 8.1 O-series):

- thermal zone `type` is "cpu-thermal"/"ddr-thermal" on mainline-based kernels (Hardkernel 6.x,
  Armbian) and "soc_thermal"/"ddr_thermal" on the Hardkernel 4.9 images. Zones are matched by
  type, never by index: thermal_zone0 is whichever sensor probed first.
- cpufreq policies are named after their first CPU (policy0 = little, policy2 = big on both
  kernel families); `related_cpus` is read rather than assumed.
- throttling has no firmware flag like the Pi's `get_throttled`; it is inferred from the cpufreq
  cooling devices (`cur_state > 0`) or from `scaling_max_freq < cpuinfo_max_freq`.
- eMMC modules of generation 5.0+ expose wear estimates in /sys/bus/mmc/devices/*/life_time.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import Collector, SysFS

_THERMAL_GLOB = "/sys/class/thermal/thermal_zone*/type"
_COOLING_GLOB = "/sys/class/thermal/cooling_device*/type"
_POLICY_GLOB = "/sys/devices/system/cpu/cpufreq/policy*/related_cpus"
_MMC_GLOB = "/sys/bus/mmc/devices/*/type"

CPU_ZONE_TYPES = ("cpu-thermal", "soc_thermal", "cpu_thermal", "soc-thermal")
DDR_ZONE_TYPES = ("ddr-thermal", "ddr_thermal")
# mainline: "cpufreq-cpu0" (older: "thermal-cpufreq-0"); Hardkernel 4.9: "cpufreq_cool0" etc.
_CPUFREQ_COOLING = re.compile(r"(cpufreq|thermal-cpufreq)", re.I)

_MB = 1024 * 1024


def detect_variant(model: Optional[str]) -> Optional[str]:
    """'Hardkernel ODROID-N2Plus' -> 'n2plus'; N2L -> 'n2l'; N2 -> 'n2'; else None."""
    text = (model or "").upper().replace(" ", "")
    if "ODROID-N2" not in text:
        return None
    if "N2PLUS" in text or "N2+" in text:
        return "n2plus"
    if "N2L" in text:
        return "n2l"
    return "n2"


def _parse_cpu_list(text: Optional[str]) -> List[int]:
    """'0 1' or '2-5' or '0-1,4' -> [0, 1] / [2, 3, 4, 5] / [0, 1, 4]"""
    cpus: List[int] = []
    for part in (text or "").replace(",", " ").split():
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                cpus.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                cpus.append(int(part))
            except ValueError:
                continue
    return sorted(set(cpus))


class OdroidN2Collector(Collector):
    name = "odroidn2"

    def __init__(self, sysfs: Optional[SysFS] = None) -> None:
        self.sysfs = sysfs or SysFS()
        self._zones: Optional[Dict[str, str]] = None  # {"cpu": "/sys/.../thermal_zoneN", ...}
        self._clusters: Optional[Dict[str, str]] = None  # {"little": policy dir, "big": ...}

    # -- discovery (cached: sysfs layout does not change while the agent runs) ---------------

    def _thermal_zones(self) -> Dict[str, str]:
        if self._zones is None:
            zones: Dict[str, str] = {}
            for path in self.sysfs.glob(_THERMAL_GLOB):
                ztype = (self.sysfs.read(path) or "").strip()
                zone_dir = path.rsplit("/", 1)[0]
                if ztype in CPU_ZONE_TYPES and "cpu" not in zones:
                    zones["cpu"] = zone_dir
                elif ztype in DDR_ZONE_TYPES and "ddr" not in zones:
                    zones["ddr"] = zone_dir
            self._zones = zones
        return self._zones

    def _cpu_clusters(self) -> Dict[str, str]:
        """Policy directories keyed little/big by cluster size (2 little A53, 4 big A73).

        Falls back to 'first policy = little' when both clusters have the same size.
        """
        if self._clusters is None:
            policies = []
            for path in self.sysfs.glob(_POLICY_GLOB):
                cpus = _parse_cpu_list(self.sysfs.read(path))
                if cpus:
                    policies.append((cpus, path.rsplit("/", 1)[0]))
            clusters: Dict[str, str] = {}
            if len(policies) >= 2:
                policies.sort(key=lambda item: (len(item[0]), item[0][0]))
                clusters["little"] = policies[0][1]
                clusters["big"] = policies[-1][1]
            elif len(policies) == 1:
                clusters["big"] = policies[0][1]
            self._clusters = clusters
        return self._clusters

    # -- Collector interface -----------------------------------------------------------------

    def static_info(self) -> Dict[str, Any]:
        model = self.sysfs.read("/proc/device-tree/model")
        clusters = self._cpu_clusters()
        info: Dict[str, Any] = {
            "variant": detect_variant(model),
            "little_cores": len(
                _parse_cpu_list(self._read_in(clusters.get("little"), "related_cpus"))
            ),
            "big_cores": len(_parse_cpu_list(self._read_in(clusters.get("big"), "related_cpus"))),
            "thermal_zone_types": {
                kind: self.sysfs.read(zone + "/type")
                for kind, zone in self._thermal_zones().items()
            },
            "emmc": self._emmc_dir() is not None,
        }
        return info

    def collect(self) -> Dict[str, Any]:
        zones = self._thermal_zones()
        out: Dict[str, Any] = {"extra": {}}
        cpu_temp = self._zone_temp(zones.get("cpu"))
        if cpu_temp is not None:
            out["temp_c"] = cpu_temp
        out["extra"]["ddr_temp_c"] = self._zone_temp(zones.get("ddr"))
        out["extra"]["cpu_freq_mhz"] = self._cluster_freqs()
        out["extra"]["thermal_throttle"] = self._throttled()
        return out

    def collect_slow(self) -> Dict[str, Any]:
        return {"extra": {"emmc_life": self._emmc_life()}}

    # -- readers -------------------------------------------------------------------------------

    def _read_in(self, directory: Optional[str], leaf: str) -> Optional[str]:
        return self.sysfs.read(directory + "/" + leaf) if directory else None

    def _zone_temp(self, zone_dir: Optional[str]) -> Optional[float]:
        if zone_dir is None:
            return None
        milli = self.sysfs.read_int(zone_dir + "/temp")
        if milli is None or not -40000 <= milli <= 150000:
            return None
        return round(milli / 1000.0, 1)

    def _cluster_freqs(self) -> Dict[str, Optional[int]]:
        freqs: Dict[str, Optional[int]] = {}
        for kind, policy in self._cpu_clusters().items():
            khz = self.sysfs.read_int(policy + "/scaling_cur_freq")
            freqs[kind] = round(khz / 1000) if khz else None
        return freqs

    def _throttled(self) -> Optional[bool]:
        """True when a cpufreq cooling device is engaged or a cluster's ceiling is capped."""
        seen = False
        for path in self.sysfs.glob(_COOLING_GLOB):
            ctype = self.sysfs.read(path) or ""
            if not _CPUFREQ_COOLING.search(ctype):
                continue
            seen = True
            state = self.sysfs.read_int(path.rsplit("/", 1)[0] + "/cur_state")
            if state is not None and state > 0:
                return True
        for policy in self._cpu_clusters().values():
            cap = self.sysfs.read_int(policy + "/scaling_max_freq")
            hw_max = self.sysfs.read_int(policy + "/cpuinfo_max_freq")
            if cap is not None and hw_max is not None:
                seen = True
                if cap < hw_max:
                    return True
        return False if seen else None

    def _emmc_dir(self) -> Optional[str]:
        for path in self.sysfs.glob(_MMC_GLOB):
            if (self.sysfs.read(path) or "").strip() == "MMC":
                return path.rsplit("/", 1)[0]
        return None

    def _emmc_life(self) -> Optional[Dict[str, Optional[int]]]:
        emmc = self._emmc_dir()
        if emmc is None:
            return None
        life = self.sysfs.read(emmc + "/life_time")  # "0x01 0x02": slc / mlc estimates, 10% steps
        pre_eol = self.sysfs.read_int(emmc + "/pre_eol_info")  # 1 normal, 2 warning, 3 urgent
        if life is None and pre_eol is None:
            return None  # eMMC < 5.0 modules have no wear data
        parts = life.split() if life else []
        return {
            "a": _hex(parts[0]) if len(parts) > 0 else None,
            "b": _hex(parts[1]) if len(parts) > 1 else None,
            "pre_eol": pre_eol,
        }


def _hex(text: str) -> Optional[int]:
    try:
        return int(text, 16)
    except ValueError:
        return None
