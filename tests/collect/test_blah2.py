import pytest

from retina_telemetry.collect.blah2 import Blah2Client, MalformedFrame, parse_frame
from tests.fakes.blah2_api import (
    ASSOCIATION,
    FakeResponse,
    FakeSession,
    empty_frame,
    frame,
)


def client(*responses, **kwargs):
    session = FakeSession(*responses)
    return Blah2Client(session=session, **kwargs), session


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
    """Detector running, nothing detected — 41 of 101 frames on a live node."""
    poll = parse_frame(empty_frame())

    assert poll.n_detections == 0
    assert poll.delay_km == []


def test_absent_adsb_key_is_none_not_empty_list():
    """The enrichment is gated entirely on truth.adsb.enabled, so an absent key
    means ADS-B is off — which is why nothing needs to read that config flag."""
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
    poll = parse_frame({"timestamp": 1753900000123.0, "delay": [], "doppler": [], "snr": []})

    assert poll.timestamp_ms == 1753900000123


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
    blah2, session = client(frame(1000))

    poll = blah2.poll_detection()

    assert poll is not None
    assert poll.timestamp_ms == 1000
    assert session.calls == ["http://127.0.0.1:3000/api/detection"]


def test_repeat_timestamp_is_deduped():
    """We poll faster than the producer, so seeing each CPI twice is normal."""
    blah2, _ = client(frame(1000))

    assert blah2.poll_detection() is not None
    assert blah2.poll_detection() is None


def test_new_timestamp_after_duplicate_is_returned():
    blah2, _ = client(frame(1000), frame(1000), frame(1500))

    assert blah2.poll_detection().timestamp_ms == 1000
    assert blah2.poll_detection() is None
    assert blah2.poll_detection().timestamp_ms == 1500


def test_poll_failure_returns_none_rather_than_raising():
    """The poll loop must never die: a dead blah2 is a payload, not an outage."""
    blah2, _ = client(ConnectionError("connection refused"))

    assert blah2.poll_detection() is None
    assert "connection refused" in blah2.last_error


def test_malformed_frame_is_discarded_without_raising():
    blah2, _ = client({"timestamp": 1000, "delay": [1.0], "doppler": [], "snr": []})

    assert blah2.poll_detection() is None
    assert "malformed frame" in blah2.last_error


# ── what the heartbeat reads ─────────────────────────────────────────


def test_poll_state_is_unknown_before_the_first_attempt():
    """Stage 2 omits NodeHealth.blah2 rather than guessing — which is what the
    field being optional is for."""
    blah2, _ = client(frame(1000))

    assert blah2.last_poll_ok is None


def test_poll_state_true_after_success():
    blah2, _ = client(frame(1000))
    blah2.poll_detection()

    assert blah2.last_poll_ok is True


def test_poll_state_false_when_unreachable():
    blah2, _ = client(ConnectionError("refused"))
    blah2.poll_detection()

    assert blah2.last_poll_ok is False


def test_http_error_counts_as_unreachable():
    blah2, _ = client(FakeResponse(status_code=503))
    blah2.poll_detection()

    assert blah2.last_poll_ok is False


def test_malformed_frame_does_not_mean_unreachable():
    """blah2-api answered; the data is the problem."""
    blah2, _ = client({"timestamp": 1000, "delay": [1.0], "doppler": [], "snr": []})
    blah2.poll_detection()

    assert blah2.last_poll_ok is True


def test_recovers_after_a_failure():
    blah2, _ = client(ConnectionError("refused"), frame(1000))

    blah2.poll_detection()
    assert blah2.last_poll_ok is False

    blah2.poll_detection()
    assert blah2.last_poll_ok is True
    assert blah2.last_error is None


def test_reports_no_spec_vocabulary():
    """Stage 1 does not know the server calls these "up" and "down"."""
    blah2, _ = client(frame(1000))
    blah2.poll_detection()

    assert blah2.last_poll_ok is True
    assert not hasattr(blah2, "liveness")


# ── scope ────────────────────────────────────────────────────────────


def test_only_the_detection_endpoint_is_polled():
    """blah2-api also serves /api/timing and the capture status endpoints, and
    the cpi total would reveal ring-buffer loss — but the spec has no field for
    any of it, so we do not collect it."""
    blah2, session = client(frame(1000), frame(2000))
    blah2.poll_detection()
    blah2.poll_detection()

    assert set(session.calls) == {"http://127.0.0.1:3000/api/detection"}


def test_close_closes_the_session():
    blah2, session = client(frame(1000))
    blah2.close()

    assert session.closed
