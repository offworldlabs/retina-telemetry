import pytest

from retina_telemetry.collect.host import HostReader, parse_throttled

# user nice system idle iowait irq softirq steal
STAT_A = "cpu  100 0 100 800 0 0 0 0\ncpu0 50 0 50 400 0 0 0 0\n"
# +100 busy, +100 idle over the interval, so 50%
STAT_B = "cpu  150 0 150 900 0 0 0 0\ncpu0 75 0 75 450 0 0 0 0\n"


def reader(tmp_path, *, stat=STAT_A, uptime="84213.55 501234.12", temp="58312", **kwargs):
    if stat is not None:
        (tmp_path / "stat").write_text(stat, encoding="utf-8")
    if uptime is not None:
        (tmp_path / "uptime").write_text(uptime, encoding="utf-8")
    if temp is not None:
        (tmp_path / "temp").write_text(temp, encoding="utf-8")
    return HostReader(
        proc_stat=tmp_path / "stat",
        proc_uptime=tmp_path / "uptime",
        thermal=tmp_path / "temp",
        disk_path=kwargs.pop("disk_path", tmp_path),
        vcgencmd=kwargs.pop("vcgencmd", None),
        **kwargs,
    )


# ── cpu ──────────────────────────────────────────────────────────────


def test_cpu_is_none_on_the_first_read(tmp_path):
    """/proc/stat is cumulative, so a percentage needs two samples. This is the
    only collection module that cannot answer from a single call."""
    assert reader(tmp_path).read().cpu_pct is None


def test_cpu_computed_from_the_delta(tmp_path):
    host = reader(tmp_path)
    host.read()

    (tmp_path / "stat").write_text(STAT_B, encoding="utf-8")

    assert host.read().cpu_pct == 50.0


def test_cpu_counts_iowait_as_idle(tmp_path):
    host = reader(tmp_path, stat="cpu  100 0 100 800 100 0 0 0\n")
    host.read()

    # +200 busy, +200 idle+iowait
    (tmp_path / "stat").write_text("cpu  200 0 200 900 200 0 0 0\n", encoding="utf-8")

    assert host.read().cpu_pct == 50.0


def test_cpu_none_when_counters_do_not_advance(tmp_path):
    host = reader(tmp_path)
    host.read()

    assert host.read().cpu_pct is None


def test_cpu_none_when_proc_stat_is_malformed(tmp_path):
    host = reader(tmp_path, stat="something else entirely\n")

    assert host.read().cpu_pct is None


def test_cpu_none_when_proc_stat_is_missing(tmp_path):
    host = reader(tmp_path)
    (tmp_path / "stat").unlink()

    assert host.read().cpu_pct is None


# ── the other reads ──────────────────────────────────────────────────


def test_temperature_converted_from_millidegrees(tmp_path):
    assert reader(tmp_path).read().temp_c == 58.3


def test_temperature_none_when_unreadable(tmp_path):
    assert reader(tmp_path, temp="not a number").read().temp_c is None


def test_uptime_is_the_hosts_not_this_process(tmp_path):
    """/proc/uptime is not namespaced, so this is host uptime. Which of the two
    the spec's uptime_s wants is still open."""
    assert reader(tmp_path).read().host_uptime_s == 84213


def test_uptime_none_when_missing(tmp_path):
    host = reader(tmp_path)
    (tmp_path / "uptime").unlink()

    assert host.read().host_uptime_s is None


def test_disk_free_is_measured_on_the_given_path(tmp_path):
    """statvfs *is* namespaced, so on `/` this would measure the container's
    overlay. Pointing it at /data is the whole point."""
    free = reader(tmp_path).read().disk_free_mb

    assert free is not None
    assert free > 0


def test_disk_free_none_for_a_missing_path(tmp_path):
    host = reader(tmp_path, disk_path=tmp_path / "absent")

    assert host.read().disk_free_mb is None


def test_every_source_absent_is_still_a_valid_snapshot(tmp_path):
    """All of them absent is a valid running state that still heartbeats."""
    host = reader(tmp_path, stat=None, uptime=None, temp=None, disk_path=tmp_path / "gone")

    snapshot = host.read()

    assert snapshot == snapshot.__class__(None, None, None, None, None)


# ── throttle flags ───────────────────────────────────────────────────


def test_throttle_absent_when_vcgencmd_is_not_installed(tmp_path):
    assert reader(tmp_path, vcgencmd="definitely-not-a-real-binary").read().throttle is None


def test_throttle_disabled_when_not_configured(tmp_path):
    assert reader(tmp_path, vcgencmd=None).read().throttle is None


def test_parse_throttled_reads_current_flags():
    flags = parse_throttled("throttled=0x5\n")

    assert flags.raw == 0x5
    assert flags.under_voltage_now
    assert flags.throttled_now
    assert not flags.arm_freq_capped_now
    assert flags.any_now


def test_parse_throttled_reads_since_boot_flags():
    """The high bits latch, which is what catches a marginal PSU that only
    browns out under load."""
    flags = parse_throttled("throttled=0x50000")

    assert not flags.any_now
    assert flags.any_since_boot
    assert flags.under_voltage_since_boot
    assert flags.throttled_since_boot


def test_parse_throttled_healthy():
    flags = parse_throttled("throttled=0x0")

    assert not flags.any_now
    assert not flags.any_since_boot


def test_parse_throttled_rejects_nonsense():
    with pytest.raises(ValueError):
        parse_throttled("throttled=banana")
