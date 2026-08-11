import pydantic
import pytest

from retina_telemetry.collect.host import HostSnapshot
from retina_telemetry.wire.heartbeat import build_heartbeat
from retina_telemetry.wire.serialise import to_wire

OWL_HOST = HostSnapshot(cpu_pct=63.8, temp_c=70.5, disk_free_mb=15743, host_uptime_s=181569)


def build(**overrides):
    return build_heartbeat(
        **{"state": "streaming", "uptime_s": 181569, "config_version": 7, **overrides}
    )


def test_uptime_is_the_devices_not_this_process_or_blah2s():
    """Three candidates exist. The heartbeat is the node's account of itself
    and "the node" is the board, so /proc/uptime is the one that belongs."""
    beat = build(uptime_s=OWL_HOST.host_uptime_s, host=OWL_HOST)

    assert beat.uptime_s == 181569
    assert beat.uptime_s == OWL_HOST.host_uptime_s


def test_uptime_is_required_so_the_caller_must_resolve_an_unreadable_one():
    """host_uptime_s is best-effort like every other host read, but uptime_s is
    required — so a None must be resolved before it gets here, not passed
    through. Falling back to this process's uptime is a true lower bound."""
    with pytest.raises(pydantic.ValidationError):
        build(uptime_s=None)


def test_the_three_required_fields_are_all_it_needs():
    """health, versions and errors are optional throughout, so a node that
    knows nothing about itself still heartbeats."""
    beat = build()

    assert beat.state == "streaming"
    assert beat.uptime_s == 181569
    assert beat.config_version == 7
    assert beat.health is None
    assert beat.versions is None


def test_every_health_field_traced_to_its_source():
    beat = build(host=OWL_HOST, blah2_up=True, adsb_present=True)

    assert beat.health.cpu_pct == 63.8  # HostSnapshot.cpu_pct
    assert beat.health.temp_c == 70.5  # HostSnapshot.temp_c
    assert beat.health.disk_free_mb == 15743  # HostSnapshot.disk_free_mb
    assert beat.health.blah2 == "up"  # Blah2Client.last_poll_ok
    assert beat.health.adsb == "up"  # DetectionPoll.adsb is not None


# ── blah2: up, down, or absent ───────────────────────────────────────


def test_blah2_down_when_the_poll_failed():
    assert build(blah2_up=False).health.blah2 == "down"


def test_blah2_omitted_before_the_first_poll():
    """None means we have not looked, which is not the same as down."""
    assert build(host=OWL_HOST, blah2_up=None).health.blah2 is None


# ── adsb: up or absent, never down ───────────────────────────────────


def test_adsb_omitted_when_association_is_switched_off():
    """An absent adsb key means the operator disabled it. Reporting "down"
    would present a deliberate configuration as a fault, and the spec has no
    vocabulary for "disabled"."""
    beat = build(host=OWL_HOST, adsb_present=False)

    assert beat.health.adsb is None


def test_adsb_is_never_reported_down():
    for present in (True, False, None):
        assert build(host=OWL_HOST, adsb_present=present).health.adsb in ("up", None)


# ── partial knowledge ────────────────────────────────────────────────


def test_a_partial_host_snapshot_reports_what_it_has():
    """Every reader is independently best-effort, so a node that can read its
    temperature but not its disk still reports the temperature."""
    partial = HostSnapshot(cpu_pct=None, temp_c=70.5, disk_free_mb=None, host_uptime_s=None)

    beat = build(host=partial)

    assert beat.health.temp_c == 70.5
    assert beat.health.cpu_pct is None
    assert "cpu_pct" not in beat.model_dump(exclude_none=True)["health"]


def test_first_read_has_no_cpu_and_that_is_not_an_error():
    """/proc/stat is cumulative, so the first HostReader.read() always returns
    cpu_pct=None."""
    first = HostSnapshot(cpu_pct=None, temp_c=70.5, disk_free_mb=15743, host_uptime_s=181569)

    assert build(host=first).health.cpu_pct is None


def test_health_omitted_entirely_when_nothing_is_known():
    empty = HostSnapshot(cpu_pct=None, temp_c=None, disk_free_mb=None, host_uptime_s=None)

    assert build(host=empty, blah2_up=None, adsb_present=None).health is None


# ── queue_depth ──────────────────────────────────────────────────────


def test_queue_depth_is_never_populated():
    """Meaningless with at most one request in flight and no queue — 0 and 1
    are the only possible values and neither tells the server anything. Q11."""
    beat = build(host=OWL_HOST, blah2_up=True)

    assert beat.health.queue_depth is None
    assert "queue_depth" not in beat.model_dump(exclude_none=True)["health"]


# ── versions ─────────────────────────────────────────────────────────


def test_versions_omitted_when_none_are_known():
    assert build().versions is None


def test_partial_versions_send_what_is_readable():
    """retina_node currently has no readable source, so this is the real case."""
    beat = build(owl_os="owl-os-pi5-v0.11.1-dev", blah2_image="v0.3.16")

    assert beat.versions.owl_os == "owl-os-pi5-v0.11.1-dev"
    assert beat.versions.blah2_image == "v0.3.16"
    assert beat.versions.retina_node is None


# ── errors ───────────────────────────────────────────────────────────


def test_errors_default_to_an_empty_list():
    assert to_wire(build())["errors"] == []


def test_errors_are_copied_not_aliased():
    """The accumulator is cleared once a beat is acknowledged; the payload must
    not change underneath a request in flight."""
    accumulated = ["detection poll failed: connection refused"]

    beat = build(errors=accumulated)
    accumulated.clear()

    assert to_wire(beat)["errors"] == ["detection poll failed: connection refused"]
