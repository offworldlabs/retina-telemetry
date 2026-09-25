"""The retina-tracker reader.

Every case here is one ``GET /frame`` answer, or a sequence of them, because
the reader's whole job is deciding what an answer means for the frame just
polled: tracks, nothing for this frame, or no tracker at all.
"""

import pytest

from retina_telemetry.collect.tracker import (
    MalformedTrackerFrame,
    TrackerClient,
    parse_tracks,
)
from tests.fakes.blah2_api import FakeResponse, FakeSession

T = 1786014064679
RUN = "20260925T101500Z-3fa9c1"

#: One active track as retina-tracker's /frame reports it, all thirteen keys.
ACTIVE = {
    "id": "260925-00001A",
    "state": "active",
    "hit": 1,
    "n_associated": 14,
    "n_missed": 0,
    "adsb_hex": "4ca1f2",
    "is_anomalous": False,
    "anomaly_types": [],
    "max_velocity_ms": 231.4,
    "born_timestamp": T - 7000,
    "avg_snr": 16.2,
    "shadow_fraction": 0.0,
    "interference_fraction": 0.0,
}


def held(*tracks, timestamp=T):
    return {"run": RUN, "timestamp": timestamp, "tracks": list(tracks)}


def not_held(latest):
    return FakeResponse({"error": "frame not held", "run": RUN, "latest": latest}, 404)


class Clock:
    """A clock that only moves when the client sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept = 0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += 1


def client(*responses, clock=None):
    clock = clock or Clock()
    session = FakeSession(*responses)
    return (
        TrackerClient("http://tracker", session=session, sleep=clock.sleep, clock=clock),
        session,
        clock,
    )


def test_a_held_frame_is_read_in_the_trackers_own_units():
    tracker, session, _ = client(held(ACTIVE))

    frame = tracker.frame(T)

    assert session.calls == ["http://tracker/frame"]
    assert session.params == [{"timestamp": T}]
    assert frame.run == RUN
    assert frame.timestamp_ms == T
    (track,) = frame.tracks
    assert track.id == "260925-00001A"
    assert track.state == "active"
    assert track.hit == 1  # an index into blah2-api's arrays, not ours yet
    assert track.born_timestamp_ms == T - 7000  # milliseconds, converted in wire/
    assert track.avg_snr_db == 16.2
    assert tracker.last_error is None


def test_an_empty_list_is_a_tracker_holding_no_track():
    """``[]`` and ``None`` mean different things on the wire, so they must
    arrive different here."""
    tracker, _, _ = client(held())

    assert tracker.frame(T).tracks == []


def test_a_tracker_not_yet_at_this_frame_is_waited_for():
    """The race this retry exists for: blah2-api stores a frame and forwards it
    in the same breath, so we can poll it before the tracker has finished."""
    tracker, session, clock = client(not_held(T - 1000), not_held(T - 1000), held(ACTIVE))

    frame = tracker.frame(T)

    assert frame.tracks is not None
    assert len(session.calls) == 3
    assert clock.slept == 2


def test_the_wait_is_bounded():
    tracker, session, clock = client(not_held(T - 1000))

    frame = tracker.frame(T)

    assert frame.run == RUN
    assert frame.tracks is None  # the tracker produced nothing for this frame
    assert clock.now == pytest.approx(0.3, abs=0.05)


def test_a_frame_the_tracker_is_past_is_not_waited_for():
    """Evicted, or skipped: either way it is not coming back."""
    tracker, session, clock = client(not_held(T + 1000))

    frame = tracker.frame(T)

    assert frame.tracks is None
    assert len(session.calls) == 1
    assert clock.slept == 0


def test_a_tracker_nobody_feeds_is_never_waited_for():
    """``tracker_forward.enabled: false`` leaves the tracker up with nothing.
    Waiting on it would delay every frame for a result that never comes."""
    tracker, session, clock = client(not_held(None))

    frame = tracker.frame(T)

    assert frame == frame.__class__(run=RUN, timestamp_ms=T, tracks=None)
    assert clock.slept == 0


def test_an_unreachable_tracker_is_no_tracker():
    tracker, _, _ = client(ConnectionError("refused at <object at 0x7f00>"))

    assert tracker.frame(T) is None
    # The type, not the text: requests' messages carry an object address, so
    # the text would be a new error on every frame.
    assert tracker.last_error == "tracker unreachable: ConnectionError"


def test_an_unexpected_status_is_no_tracker():
    tracker, _, _ = client(FakeResponse({"error": "boom"}, 500))

    assert tracker.frame(T) is None
    assert tracker.last_error == "tracker answered 500"


def test_an_answer_without_a_run_is_no_tracker():
    """Tracks cannot be sent without the run that scopes their ids."""
    tracker, _, _ = client({"timestamp": T, "tracks": []})

    assert tracker.frame(T) is None


def test_a_malformed_track_costs_the_frames_tracks_not_the_tracker():
    broken = {**ACTIVE, "hit": "1"}
    tracker, _, _ = client(held(broken))

    frame = tracker.frame(T)

    assert frame.run == RUN
    assert frame.tracks is None
    assert "hit must be an integer" in tracker.last_error


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": 7}, "id must be a string"),
        ({"n_associated": 1.5}, "n_associated must be an integer"),
        ({"hit": True}, "hit must be an integer"),
        ({"anomaly_types": ["orbit", 3]}, "anomaly_types must be strings"),
        ({"anomaly_types": None}, "anomaly_types must be strings"),
        ({"is_anomalous": None}, "is_anomalous must be a boolean"),
    ],
)
def test_parse_refuses_what_it_cannot_vouch_for(change, message):
    with pytest.raises(MalformedTrackerFrame, match=message):
        parse_tracks([{**ACTIVE, **change}])


def test_parse_names_a_missing_key():
    entry = {k: v for k, v in ACTIVE.items() if k != "n_missed"}

    with pytest.raises(MalformedTrackerFrame, match="missing n_missed"):
        parse_tracks([entry])


def test_a_non_finite_optional_number_becomes_unknown():
    """NaN is not valid JSON on the way out, so it never reaches wire/."""
    (track,) = parse_tracks([{**ACTIVE, "avg_snr": float("nan")}])

    assert track.avg_snr_db is None


@pytest.mark.parametrize("value", [None, float("inf"), "231"])
def test_a_required_number_is_never_substituted(value):
    """A speed the tracker did not give us is not zero."""
    with pytest.raises(MalformedTrackerFrame, match="max_velocity_ms must be a finite number"):
        parse_tracks([{**ACTIVE, "max_velocity_ms": value}])
