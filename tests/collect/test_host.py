from retina_telemetry.collect.host import HostReader

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

    assert snapshot == snapshot.__class__(None, None, None, None)


def test_collects_only_what_the_spec_asks_for(tmp_path):
    """NodeHealth has cpu_pct, temp_c and disk_free_mb; HeartbeatRequest has
    uptime_s. Pi throttle flags were dropped for having no field to go in."""
    assert set(vars(reader(tmp_path).read())) == {
        "cpu_pct",
        "temp_c",
        "disk_free_mb",
        "host_uptime_s",
    }
