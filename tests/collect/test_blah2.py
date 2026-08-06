import pytest

from retina_telemetry.collect.blah2 import (
    Blah2Client,
    Liveness,
    MalformedFrame,
    parse_frame,
)
from tests.fakes.blah2_api import (
    ASSOCIATION,
    FakeClock,
    FakeResponse,
    FakeSession,
    empty_frame,
    frame,
)

CPI_S = 0.5


def client(*responses, cpi_s=CPI_S, clock=None, **kwargs):
    clock = clock or FakeClock()
    session = FakeSession(*responses)
    return Blah2Client(cpi_s=cpi_s, session=session, clock=clock, **kwargs), session, clock


# ── parse_frame ──────────────────────────────────────────────────────


def test_parses_a_frame_without_conversion():
    """Stage 1 hands over blah2's own units. Converting here would put the
    numbers out of step with what data-sources.md documents."""
    poll = parse_frame(frame(1753900000123, delay=[12.4, 30.1], snr=[14.2, 9.8]))

    assert poll.timestamp_ms == 1753900000123  # milliseconds, not seconds
    assert poll.delay_km == [12.4, 30.1]  # kilometres, not microseconds
    assert poll.snr_db == [14.2, 9.8]
    assert poll.n_detections == 2


def test_empty_frame_is_valid():
    poll = parse_frame(empty_frame())

    assert poll.is_empty
    assert poll.n_detections == 0
    assert poll.delay_km == []


def test_absent_adsb_key_is_none_not_empty_list():
    """`None` means ADS-B is disabled, so stage 2 must synthesise [None] * n.
    An empty list would mean ADS-B is on and the frame has no detections."""
    assert parse_frame(frame()).adsb is None


def test_adsb_associations_pass_through_as_objects():
    """blah2-api emits objects, not hex strings. Mapping down to `.hex` is
    stage 2's job, so the residuals survive this layer."""
    poll = parse_frame(frame(adsb=[ASSOCIATION, None]))

    assert poll.adsb == [ASSOCIATION, None]
    assert poll.adsb[0]["hex"] == "4ca1f2"


def test_empty_frame_with_adsb_enabled_keeps_empty_list():
    poll = parse_frame(frame(delay=[], doppler=[], snr=[], adsb=[]))

    assert poll.adsb == []  # distinct from None


def test_unequal_array_lengths_rejected():
    """blah2 builds these from one loop so they are equal by construction, but
    nothing validates it and a desync would read out of bounds silently."""
    with pytest.raises(MalformedFrame, match="disagree in length"):
        parse_frame(frame(delay=[1.0, 2.0], doppler=[1.0], snr=[1.0, 2.0]))


def test_adsb_length_mismatch_rejected():
    with pytest.raises(MalformedFrame, match="adsb has 1 entries"):
        parse_frame(frame(delay=[1.0, 2.0], doppler=[1.0, 2.0], snr=[1.0, 2.0], adsb=[None]))


@pytest.mark.parametrize(
    "payload",
    [
        {"delay": [], "doppler": [], "snr": []},  # no timestamp
        {"timestamp": "1753900000123", "delay": [], "doppler": [], "snr": []},
        {"timestamp": True, "delay": [], "doppler": [], "snr": []},
        {"timestamp": 1753900000123.5, "delay": [], "doppler": [], "snr": []},
    ],
)
def test_bad_timestamps_rejected(payload):
    with pytest.raises(MalformedFrame):
        parse_frame(payload)


def test_integral_float_timestamp_accepted():
    """A type surprise on a real node should not stop the stream outright."""
    assert (
        parse_frame(
            {"timestamp": 1753900000123.0, "delay": [], "doppler": [], "snr": []}
        ).timestamp_ms
        == 1753900000123
    )


def test_non_numeric_detection_values_rejected():
    with pytest.raises(MalformedFrame, match="non-numeric"):
        parse_frame(frame(delay=["12.4", 30.1]))


def test_missing_array_rejected():
    with pytest.raises(MalformedFrame, match="doppler must be an array"):
        parse_frame({"timestamp": 1, "delay": [], "snr": []})


def test_non_object_payload_rejected():
    with pytest.raises(MalformedFrame, match="expected a JSON object"):
        parse_frame([1, 2, 3])


# ── polling and dedupe ───────────────────────────────────────────────


def test_poll_returns_the_frame():
    blah2, session, _ = client(frame(1000))

    poll = blah2.poll_detection()

    assert poll is not None
    assert poll.timestamp_ms == 1000
    assert session.calls == ["http://127.0.0.1:3000/api/detection"]


def test_repeat_timestamp_is_deduped():
    """Polling at ~4 Hz against a 2 Hz producer means seeing each CPI twice."""
    blah2, _, _ = client(frame(1000))

    assert blah2.poll_detection() is not None
    assert blah2.poll_detection() is None


def test_new_timestamp_after_duplicate_is_returned():
    blah2, _, _ = client(frame(1000), frame(1000), frame(1500))

    assert blah2.poll_detection().timestamp_ms == 1000
    assert blah2.poll_detection() is None
    assert blah2.poll_detection().timestamp_ms == 1500


def test_poll_failure_returns_none_rather_than_raising():
    """The poll loop must never die: a dead blah2 is a payload, not an outage."""
    blah2, _, _ = client(ConnectionError("connection refused"))

    assert blah2.poll_detection() is None
    assert "connection refused" in blah2.last_error


def test_http_error_is_a_failure():
    blah2, _, _ = client(FakeResponse(status_code=503))

    assert blah2.poll_detection() is None
    assert blah2.liveness is Liveness.DOWN


def test_malformed_frame_is_discarded_without_raising():
    blah2, _, _ = client({"timestamp": 1000, "delay": [1.0], "doppler": [], "snr": []})

    assert blah2.poll_detection() is None
    assert "malformed frame" in blah2.last_error


def test_consecutive_failures_counted_then_reset():
    blah2, _, _ = client(OSError("boom"), OSError("boom"), frame(1000))

    blah2.poll_detection()
    blah2.poll_detection()
    assert blah2.consecutive_failures == 2

    blah2.poll_detection()
    assert blah2.consecutive_failures == 0
    assert blah2.last_error is None


# ── liveness ─────────────────────────────────────────────────────────


def test_liveness_unknown_before_first_poll():
    blah2, _, _ = client(frame(1000))

    assert blah2.liveness is Liveness.UNKNOWN


def test_liveness_up_after_a_good_poll():
    blah2, _, _ = client(frame(1000))
    blah2.poll_detection()

    assert blah2.liveness is Liveness.UP


def test_liveness_down_when_the_poll_fails():
    blah2, _, _ = client(ConnectionError("refused"))
    blah2.poll_detection()

    assert blah2.liveness is Liveness.DOWN


def test_liveness_wedged_when_timestamp_stops_advancing():
    """The state container health cannot see: blah2 is up, answering, and
    returning the same CPI forever."""
    clock = FakeClock()
    blah2, _, _ = client(frame(1000), clock=clock)

    blah2.poll_detection()
    assert blah2.liveness is Liveness.UP

    clock.advance(10 * CPI_S + 0.1)  # ten CPIs with no new frame
    blah2.poll_detection()

    assert blah2.liveness is Liveness.WEDGED


def test_liveness_stays_up_within_the_stale_window():
    clock = FakeClock()
    blah2, _, _ = client(frame(1000), clock=clock)
    blah2.poll_detection()

    clock.advance(10 * CPI_S - 0.1)

    assert blah2.liveness is Liveness.UP


def test_liveness_recovers_when_frames_resume():
    clock = FakeClock()
    blah2, _, _ = client(frame(1000), frame(1000), frame(2000), clock=clock)

    blah2.poll_detection()
    clock.advance(10 * CPI_S + 0.1)
    blah2.poll_detection()
    assert blah2.liveness is Liveness.WEDGED

    blah2.poll_detection()
    assert blah2.liveness is Liveness.UP


def test_persistent_malformed_frames_read_as_wedged_not_down():
    """blah2-api is answering, so it is not down — but nothing usable is
    coming out, which is exactly what wedged means."""
    clock = FakeClock()
    bad = {"timestamp": 1000, "delay": [1.0], "doppler": [], "snr": []}
    blah2, _, _ = client(bad, clock=clock)

    blah2.poll_detection()
    assert blah2.liveness is Liveness.UP  # inside the grace period

    clock.advance(10 * CPI_S + 0.1)
    assert blah2.liveness is Liveness.WEDGED


def test_stale_window_has_a_floor_for_short_cpis():
    """A very short CPI must not make the check hair-trigger."""
    clock = FakeClock()
    blah2, _, _ = client(frame(1000), cpi_s=0.01, clock=clock)
    blah2.poll_detection()

    clock.advance(2.0)  # far beyond 10 * 0.01, but under the 3 s floor

    assert blah2.liveness is Liveness.UP


# ── scope ────────────────────────────────────────────────────────────


def test_only_the_detection_endpoint_is_polled():
    """blah2-api also serves /api/timing and the capture status endpoints, and
    the cpi total would reveal ring-buffer loss — but the spec has no field for
    any of it, so we do not collect it."""
    blah2, session, _ = client(frame(1000), frame(2000))
    blah2.poll_detection()
    blah2.poll_detection()

    assert set(session.calls) == {"http://127.0.0.1:3000/api/detection"}


def test_close_closes_the_session():
    blah2, session, _ = client(frame(1000))
    blah2.close()

    assert session.closed
