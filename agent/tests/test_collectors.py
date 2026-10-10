from cluster_agent.collectors import MetricsCollector, detect_board
from cluster_agent.collectors.base import SysFS
from cluster_agent.collectors.common import CommonCollector
from cluster_agent.collectors.odroid import OdroidN2Collector, detect_variant
from cluster_agent.collectors.rdkx3 import RdkX3Collector, parse_hrut_somstatus
from cluster_agent.collectors.rpi import (
    RpiCollector,
    decode_throttled,
    parse_throttled,
    parse_volts,
)

# Shape of hrut_somstatus output as documented for RDK X3. Replace with a real capture in
# Phase 0 if the installed image prints something different.
HRUT_OUTPUT = """\
=====================1=====================
temperature-->
        CPU      : 51.7 (C)
cpu frequency-->
              min       cur     max
        cpu0: 240000    1200000 1200000
        cpu1: 240000    1200000 1200000
bpu status information---->
             min        cur             max             ratio
        bpu0: 400000000 1000000000      1000000000      37
        bpu1: 400000000 1000000000      1000000000      5
=====================2=====================
temperature-->
        CPU      : 99.9 (C)
bpu status information---->
        bpu0: 400000000 1000000000      1000000000      99
"""


# -- Raspberry Pi -----------------------------------------------------------------


def test_decode_throttled_splits_now_and_since_boot():
    flags = decode_throttled(0x50005)
    assert flags["raw"] == "0x50005"
    assert flags["now"] == {
        "under_voltage": True,
        "freq_capped": False,
        "throttled": True,
        "soft_temp_limit": False,
    }
    assert flags["since_boot"]["under_voltage"] is True
    assert flags["since_boot"]["throttled"] is True
    assert flags["since_boot"]["freq_capped"] is False


def test_parse_vcgencmd_outputs():
    assert parse_throttled("throttled=0x0\n") == 0
    assert parse_throttled("throttled=0x50000") == 0x50000
    assert parse_throttled("garbage") is None
    assert parse_throttled(None) is None
    assert parse_volts("volt=1.2000V\n") == 1.2
    assert parse_volts("") is None


def test_rpi_collector_uses_vcgencmd(make_sysfs, fake_runner):
    runner = fake_runner(
        {
            ("vcgencmd", "get_throttled"): "throttled=0x1\n",
            ("vcgencmd", "measure_volts", "core"): "volt=1.2875V\n",
        }
    )
    c = RpiCollector(make_sysfs({}), runner)
    assert c.collect()["extra"]["throttled"] == "0x1"
    assert c.collect_slow()["extra"]["core_volts"] == 1.2875


def test_rpi_collector_falls_back_to_firmware_sysfs(make_sysfs, fake_runner):
    sysfs = make_sysfs({"/sys/devices/platform/soc/soc:firmware/get_throttled": "50000\n"})
    c = RpiCollector(sysfs, fake_runner())  # vcgencmd unavailable
    assert c.collect()["extra"]["throttled"] == "0x50000"


def test_rpi_collector_without_any_source(make_sysfs, fake_runner):
    c = RpiCollector(make_sysfs({}), fake_runner())
    assert c.collect() == {"extra": {"throttled": None}}


# -- RDK X3 -----------------------------------------------------------------------


def test_parse_hrut_uses_first_block_only():
    parsed = parse_hrut_somstatus(HRUT_OUTPUT)
    assert parsed == {"temp_c": 51.7, "bpu": [37, 5]}


def test_parse_hrut_handles_missing_output():
    assert parse_hrut_somstatus(None) == {"temp_c": None, "bpu": None}
    assert parse_hrut_somstatus("unexpected text") == {"temp_c": None, "bpu": None}


def test_rdkx3_reads_sysfs_without_spawning_tool(make_sysfs, fake_runner):
    sysfs = make_sysfs(
        {
            "/sys/devices/system/bpu/bpu0/ratio": "12\n",
            "/sys/devices/system/bpu/bpu1/ratio": "0\n",
            "/sys/class/hwmon/hwmon0/temp1_input": "48250\n",
        }
    )
    runner = fake_runner()
    c = RdkX3Collector(sysfs, runner)
    c.collect_slow()
    assert c.collect() == {"extra": {"bpu": [12, 0]}, "temp_c": 48.2}
    assert c.static_info() == {"bpu_cores": 2}
    assert runner.calls == []


def test_rdkx3_falls_back_to_hrut_on_slow_cycle(make_sysfs, fake_runner):
    runner = fake_runner({("hrut_somstatus",): HRUT_OUTPUT})
    c = RdkX3Collector(make_sysfs({}), runner)
    assert c.collect() == {"extra": {"bpu": None}}  # nothing cached yet, no tool spawned
    assert runner.calls == []
    c.collect_slow()
    assert c.collect() == {"extra": {"bpu": [37, 5]}, "temp_c": 51.7}


def test_rdkx3_stops_trying_missing_tool(make_sysfs, fake_runner):
    runner = fake_runner()  # hrut_somstatus not installed
    c = RdkX3Collector(make_sysfs({}), runner)
    for _ in range(10):
        c.collect_slow()
    assert len(runner.calls) == RdkX3Collector.MAX_HRUT_FAILURES


# -- detection and merging --------------------------------------------------------


def test_detect_board(make_sysfs, tmp_path):
    assert (
        detect_board(make_sysfs({"/proc/device-tree/model": "Raspberry Pi 3 Model B Rev 1.2\x00"}))
        == "rpi3"
    )


def test_detect_rdkx3_by_bpu_sysfs(make_sysfs):
    assert detect_board(make_sysfs({"/sys/devices/system/bpu/bpu0/ratio": "0"})) == "rdkx3"


def test_detect_generic(make_sysfs):
    assert detect_board(make_sysfs({})) == "generic"


def test_common_static_info_parses_os_and_cpu(make_sysfs):
    sysfs = make_sysfs(
        {
            "/etc/os-release": 'NAME="Debian"\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n',
            "/proc/cpuinfo": "processor\t: 0\nBogoMIPS\t: 38.40\n\nHardware\t: BCM2835\n"
            "Model\t\t: Raspberry Pi 3 Model B Rev 1.2\n",
            "/proc/device-tree/model": "Raspberry Pi 3 Model B Rev 1.2\x00",
        }
    )
    info = CommonCollector(sysfs).static_info()
    assert info["os"] == "Debian GNU/Linux 12 (bookworm)"
    assert info["cpu_model"] == "Raspberry Pi 3 Model B Rev 1.2"
    assert info["device_model"] == "Raspberry Pi 3 Model B Rev 1.2"


def test_common_rates_from_counter_deltas(monkeypatch, make_sysfs):
    import cluster_agent.collectors.common as common

    class Nic:
        def __init__(self, rx, tx):
            self.bytes_recv, self.bytes_sent = rx, tx

    counters = iter(
        [{"eth0": Nic(1000, 500), "lo": Nic(0, 0)}, {"eth0": Nic(6000, 300), "lo": Nic(9, 9)}]
    )  # tx went backwards: reset
    monkeypatch.setattr(common.psutil, "net_io_counters", lambda pernic: next(counters))
    clock = iter([100.0, 105.0])
    c = CommonCollector(make_sysfs({}), clock=lambda: next(clock))
    assert c._net(next(clock)) == {}
    assert c._net(next(clock)) == {"eth0": {"rx_bps": 1000, "tx_bps": 0}}


def test_metrics_collector_on_this_host_returns_full_sample():
    mc = MetricsCollector(board="generic", sysfs=SysFS("/"))
    info = mc.static_info()
    assert info["board"] == "generic"
    assert info["cpu_count"] >= 1
    sample = mc.collect()
    for key in ("cpu", "mem", "swap", "disk", "net", "uptime_s", "procs", "ts", "extra", "top"):
        assert key in sample, key
    assert 0 <= sample["mem"]["percent"] <= 100


def test_board_temp_overrides_generic_but_none_does_not_erase(make_sysfs, fake_runner):
    sysfs = make_sysfs({"/sys/class/thermal/thermal_zone0/temp": "40000"})
    mc = MetricsCollector(board="rdkx3", sysfs=sysfs, run_cmd=fake_runner())
    sample = mc.collect()
    # no hwmon/BPU data and no hrut: the generic reading (psutil or thermal zone) survives
    assert sample["extra"]["bpu"] is None
    assert sample.get("temp_c") is not None


def test_slow_metrics_only_every_nth_sample():
    mc = MetricsCollector(board="rpi3", mock_name="rpi3-01", slow_every=3)
    has_top = ["top" in mc.collect() for _ in range(6)]
    assert has_top == [True, False, False, True, False, False]


def test_mock_collector_shapes():
    rdk = MetricsCollector(board="rdkx3", mock_name="rdkx3-02")
    assert rdk.static_info()["board"] == "rdkx3"
    assert len(rdk.collect()["extra"]["bpu"]) == 2
    pi = MetricsCollector(board="rpi3", mock_name="rpi3-01")
    assert pi.collect()["extra"]["throttled"] == "0x0"


# -- ODROID-N2 family ----------------------------------------------------------------


def _n2_mainline(extra=None):
    """sysfs as a mainline-based kernel (Hardkernel 6.x, Armbian) lays it out."""
    files = {
        "/proc/device-tree/model": "Hardkernel ODROID-N2Plus\x00",
        # probe order puts DDR first on this boot: zones must be matched by type
        "/sys/class/thermal/thermal_zone0/type": "ddr-thermal\n",
        "/sys/class/thermal/thermal_zone0/temp": "41000\n",
        "/sys/class/thermal/thermal_zone1/type": "cpu-thermal\n",
        "/sys/class/thermal/thermal_zone1/temp": "52300\n",
        "/sys/class/thermal/cooling_device0/type": "gpio-fan\n",
        "/sys/class/thermal/cooling_device0/cur_state": "1\n",
        "/sys/class/thermal/cooling_device1/type": "cpufreq-cpu0\n",
        "/sys/class/thermal/cooling_device1/cur_state": "0\n",
        "/sys/class/thermal/cooling_device2/type": "cpufreq-cpu2\n",
        "/sys/class/thermal/cooling_device2/cur_state": "0\n",
        "/sys/devices/system/cpu/cpufreq/policy0/related_cpus": "0 1\n",
        "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq": "1896000\n",
        "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq": "1896000\n",
        "/sys/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq": "1896000\n",
        "/sys/devices/system/cpu/cpufreq/policy2/related_cpus": "2 3 4 5\n",
        "/sys/devices/system/cpu/cpufreq/policy2/scaling_cur_freq": "2208000\n",
        "/sys/devices/system/cpu/cpufreq/policy2/scaling_max_freq": "2208000\n",
        "/sys/devices/system/cpu/cpufreq/policy2/cpuinfo_max_freq": "2208000\n",
        "/sys/bus/mmc/devices/mmc1:0001/type": "MMC\n",
        "/sys/bus/mmc/devices/mmc1:0001/life_time": "0x01 0x02\n",
        "/sys/bus/mmc/devices/mmc1:0001/pre_eol_info": "0x01\n",
        "/sys/bus/mmc/devices/mmc0:aaaa/type": "SD\n",
    }
    files.update(extra or {})
    return files


def test_detect_variant_strings():
    assert detect_variant("Hardkernel ODROID-N2") == "n2"
    assert detect_variant("Hardkernel ODROID-N2Plus") == "n2plus"
    assert detect_variant("Hardkernel ODROID-N2L") == "n2l"
    assert detect_variant("Raspberry Pi 3 Model B") is None
    assert detect_variant(None) is None


def test_detect_board_odroid(make_sysfs):
    for model in ("Hardkernel ODROID-N2", "Hardkernel ODROID-N2Plus\x00", "Hardkernel ODROID-N2L"):
        assert detect_board(make_sysfs({"/proc/device-tree/model": model})) == "odroidn2"


def test_odroid_mainline_layout(make_sysfs):
    c = OdroidN2Collector(make_sysfs(_n2_mainline()))
    info = c.static_info()
    assert info["variant"] == "n2plus"
    assert info["little_cores"] == 2 and info["big_cores"] == 4
    assert info["thermal_zone_types"] == {"cpu": "cpu-thermal", "ddr": "ddr-thermal"}
    assert info["emmc"] is True
    sample = c.collect()
    assert sample["temp_c"] == 52.3  # the CPU zone, although it is thermal_zone1 here
    assert sample["extra"]["ddr_temp_c"] == 41.0
    assert sample["extra"]["cpu_freq_mhz"] == {"little": 1896, "big": 2208}
    assert sample["extra"]["thermal_throttle"] is False  # the fan is on, cpufreq is not capped
    assert c.collect_slow() == {"extra": {"emmc_life": {"a": 1, "b": 2, "pre_eol": 1}}}


def test_odroid_detects_throttling(make_sysfs):
    capped = OdroidN2Collector(
        make_sysfs(_n2_mainline({"/sys/class/thermal/cooling_device2/cur_state": "3\n"}))
    )
    assert capped.collect()["extra"]["thermal_throttle"] is True
    ceiling = OdroidN2Collector(
        make_sysfs(
            _n2_mainline({"/sys/devices/system/cpu/cpufreq/policy2/scaling_max_freq": "1800000\n"})
        )
    )
    assert ceiling.collect()["extra"]["thermal_throttle"] is True


def test_odroid_hardkernel_49_layout(make_sysfs):
    """Hardkernel 4.9 images: soc_thermal/ddr_thermal names, cpufreq_cool devices, no eMMC data."""
    files = {
        "/proc/device-tree/model": "Hardkernel ODROID-N2\x00",
        "/sys/class/thermal/thermal_zone0/type": "soc_thermal\n",
        "/sys/class/thermal/thermal_zone0/temp": "61000\n",
        "/sys/class/thermal/thermal_zone1/type": "ddr_thermal\n",
        "/sys/class/thermal/thermal_zone1/temp": "48000\n",
        "/sys/class/thermal/cooling_device0/type": "thermal-cpufreq-0\n",
        "/sys/class/thermal/cooling_device0/cur_state": "2\n",
        "/sys/devices/system/cpu/cpufreq/policy0/related_cpus": "0-1\n",
        "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq": "1000000\n",
        "/sys/devices/system/cpu/cpufreq/policy2/related_cpus": "2-5\n",
        "/sys/devices/system/cpu/cpufreq/policy2/scaling_cur_freq": "1800000\n",
        "/sys/bus/mmc/devices/mmc1:0001/type": "MMC\n",  # eMMC 4.5 module: no life_time
    }
    c = OdroidN2Collector(make_sysfs(files))
    assert c.static_info()["variant"] == "n2"
    sample = c.collect()
    assert sample["temp_c"] == 61.0 and sample["extra"]["ddr_temp_c"] == 48.0
    assert sample["extra"]["cpu_freq_mhz"] == {"little": 1000, "big": 1800}
    assert sample["extra"]["thermal_throttle"] is True
    assert c.collect_slow() == {"extra": {"emmc_life": None}}


def test_odroid_without_sysfs_reports_nulls(make_sysfs):
    c = OdroidN2Collector(make_sysfs({"/proc/device-tree/model": "Hardkernel ODROID-N2L"}))
    info = c.static_info()
    assert info["variant"] == "n2l" and info["emmc"] is False and info["big_cores"] == 0
    sample = c.collect()
    assert "temp_c" not in sample
    assert sample["extra"] == {"ddr_temp_c": None, "cpu_freq_mhz": {}, "thermal_throttle": None}


def test_metrics_collector_wires_odroid(make_sysfs):
    mc = MetricsCollector(board="auto", sysfs=make_sysfs(_n2_mainline()))
    assert mc.board == "odroidn2"
    assert [c.name for c in mc.collectors] == ["common", "odroidn2"]
    info = mc.static_info()
    assert info["board"] == "odroidn2" and info["variant"] == "n2plus"


def test_mock_odroid_profile():
    mc = MetricsCollector(board="odroidn2", mock_name="odroidn2-01")
    info = mc.static_info()
    assert info["cpu_count"] == 6 and info["variant"] == "n2plus" and info["bpu_cores"] is None
    sample = mc.collect()
    assert set(sample["extra"]) >= {"ddr_temp_c", "cpu_freq_mhz", "thermal_throttle"}
