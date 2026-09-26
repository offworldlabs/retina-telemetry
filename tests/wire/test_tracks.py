"""Tracks on the detection frame, contract 1.6.0.

The frame and its tracks are one unit on the server: a track that does not add
up refuses the whole frame. So every frame built here is also put through the
mock's copy of the server's rules (``_track_failures``, word for word from
``_tracks_name_this_frames_detections``) as well as the specific assertion.
"""

import dataclasses

import pytest

from retina_telemetry.collect.blah2 import DetectionPoll
from retina_telemetry.collect.tracker import TrackerFrame, TrackRaw
from retina_telemetry.wire.detection import build_detection_frame
from retina_telemetry.wire.serialise import to_wire
from retina_telemetry.wire.tracks import MAX_TRACKS, TrackLedger
from tools.mock_server import _track_failures

T = 1786014064679
RUN = "20260925T101500Z-3fa9c1"


def poll(**overrides) -> DetectionPoll:
    return DetectionPoll(
        **{
            "timestamp_ms": T,
            "delay_km": [12.4, 30.1, 55.0],
            "doppler_hz": [-118.0, 44.5, 10.0],
            "snr_db": [14.2, 9.8, 11.0],
            "adsb": None,
            **overrides,
        }
    )


def raw(track_id="260925-00001A", state="active", hit=1, **overrides) -> TrackRaw:
    return TrackRaw(
        **{
            "id": track_id,
            "state": state,
            "hit": hit if state == "active" else None,
            "n_associated": 14,
            "n_missed": 0 if state == "active" else 2,
            "adsb_hex": "4ca1f2",
            "is_anomalous": False,
            "anomaly_types": [],
            "max_velocity_ms": 231.4,
            "born_timestamp_ms": T - 7000,
            "avg_snr_db": 16.2,
            "shadow_fraction": 0.0,
            "interference_fraction": None,
            **overrides,
        }
    )


def tracked(*tracks, run=RUN, timestamp=T):
    return TrackerFrame(run=run, timestamp_ms=timestamp, tracks=list(tracks))


def build(tracker, ledger=None, **poll_overrides):
    frame = build_detection_frame(
        poll(**poll_overrides),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=7,
        tracker=tracker,
        ledger=ledger,
    )
    body = to_wire(frame)
    assert _track_failures(body) == [], "the server would refuse this frame whole"
    return body


def states(body):
    return {t["id"]: t["state"] for t in body["tracks"]}


# ── the three shapes a frame can take ────────────────────────────────


def test_no_tracker_sends_neither_field():
    """How a node says it sends no tracks."""
    body = build(None)

    assert "tracker" not in body
    assert "tracks" not in body


def test_a_tracker_with_nothing_for_this_frame_sends_null_tracks():
    """``tracks: null`` beside a ``tracker`` says the tracker produced nothing
    for this frame, and nothing about its tracks. to_wire drops it as an
    optional null, which the server reads identically: ``tracks`` defaults to
    None there, and it is ``tracker`` alone that says a tracker is running."""
    body = build(TrackerFrame(run=RUN, timestamp_ms=T, tracks=None))

    assert body["tracker"] == {"run": RUN}
    assert "tracks" not in body


def test_a_tracker_holding_no_track_sends_an_empty_list():
    body = build(tracked())

    assert body["tracker"] == {"run": RUN}
    assert body["tracks"] == []


# ── one track, field by field ────────────────────────────────────────


def test_every_track_field_traced_to_its_source():
    body = build(tracked(raw()))

    (track,) = body["tracks"]
    assert track == {
        "id": "260925-00001A",
        "state": "active",
        "hit": 1,
        "n_associated": 14,
        "n_missed": 0,
        "adsb_hex": "4ca1f2",
        "is_anomalous": False,
        "anomaly_types": [],
        "max_velocity_ms": 231.4,
        "born_t": 1786014057.679,  # born_timestamp_ms ÷ 1000
        "avg_snr": 16.2,
        "shadow_fraction": 0.0,
        # interference_fraction is optional and unknown here, so it is absent
        # rather than null: to_wire's rule reaches into the list.
    }


def test_required_nulls_survive_inside_a_track():
    """``hit`` and ``adsb_hex`` are required and nullable. Dropping either key
    is a payload the server refuses."""
    body = build(tracked(raw(state="coasting", adsb_hex=None)))

    (track,) = body["tracks"]
    assert track["hit"] is None
    assert track["adsb_hex"] is None


# ── hit is an index into what this frame sends ───────────────────────


def test_a_hit_is_renumbered_past_a_dropped_detection():
    """blah2-api's index 2 is this frame's index 1 once the non-finite
    detection at index 0 is dropped. Sending 2 would name the wrong detection,
    or one past the end."""
    body = build(tracked(raw(hit=2)), snr_db=[float("-inf"), 9.8, 11.0])

    assert body["delay"] == [100.403, 183.46]
    assert body["tracks"][0]["hit"] == 1


def test_an_active_track_whose_detection_was_not_sent_is_left_out():
    """It must name a detection this frame carries, and it is not coasting,
    so it is not sent on this frame at all."""
    body = build(
        tracked(raw(hit=0), raw("260925-00001B", hit=2)), snr_db=[float("-inf"), 9.8, 11.0]
    )

    assert states(body) == {"260925-00001B": "active"}


def test_a_track_left_out_for_one_frame_is_not_declared_dead():
    ledger = TrackLedger()
    build(tracked(raw(hit=0)), ledger)

    body = build(tracked(raw(hit=0)), ledger, snr_db=[float("nan"), 9.8, 11.0])
    assert body["tracks"] == []  # left out, not deleted

    body = build(tracked(raw(hit=1)), ledger)
    assert states(body) == {"260925-00001A": "active"}


def test_a_hit_past_the_specs_512_is_not_sent():
    n = 600
    body = build(
        tracked(raw(hit=550), raw("260925-00001B", hit=3)),
        delay_km=[10.0] * n,
        doppler_hz=[1.0] * n,
        snr_db=[10.0] * n,
    )

    assert states(body) == {"260925-00001B": "active"}


# ── deleted, exactly once ────────────────────────────────────────────


def test_a_death_the_tracker_reports_carries_its_final_values():
    ledger = TrackLedger()
    build(tracked(raw()), ledger)

    body = build(tracked(raw(state="deleted", n_missed=11)), ledger)

    (track,) = body["tracks"]
    assert track["state"] == "deleted"
    assert track["hit"] is None
    assert track["n_missed"] == 11


def test_a_death_between_two_polls_is_still_sent_once():
    """The reason the ledger exists. Latest-wins skipped the frame the track
    died on, so the tracker's next answer simply lacks it."""
    ledger = TrackLedger()
    build(tracked(raw()), ledger)

    body = build(tracked(), ledger)
    (track,) = body["tracks"]
    assert track["id"] == "260925-00001A"
    assert track["state"] == "deleted"
    assert track["hit"] is None
    assert track["n_associated"] == 14  # the last thing sent about it

    assert build(tracked(), ledger)["tracks"] == []  # and never again


def test_a_track_never_sent_is_not_sent_dead():
    """Born and died between two polls: the server never heard of it, so a
    deletion would be its first and only mention."""
    body = build(tracked(raw(state="deleted")), TrackLedger())

    assert body["tracks"] == []


def test_a_frame_with_nothing_held_neither_adds_nor_deletes():
    ledger = TrackLedger()
    build(tracked(raw()), ledger)

    build(TrackerFrame(run=RUN, timestamp_ms=T, tracks=None), ledger)

    assert states(build(tracked(raw()), ledger)) == {"260925-00001A": "active"}


def test_no_tracker_for_a_while_forgets_nothing():
    """A tracker that timed out once is still the same run when it answers."""
    ledger = TrackLedger()
    build(tracked(raw()), ledger)
    build(None, ledger)

    body = build(tracked(), ledger)

    assert states(body) == {"260925-00001A": "deleted"}


def test_a_new_run_forgets_the_old_runs_tracks():
    """Its ids are a different namespace. Deleting them under the new run
    would name tracks the new run never had, or worse, ones it does."""
    ledger = TrackLedger()
    build(tracked(raw()), ledger)

    body = build(tracked(run="20260925T111500Z-000000"), ledger)

    assert body["tracker"] == {"run": "20260925T111500Z-000000"}
    assert body["tracks"] == []


# ── the spec's bounds ────────────────────────────────────────────────


def test_active_tracks_go_first_and_a_deletion_that_does_not_fit_waits():
    ledger = TrackLedger()
    build(tracked(raw("dying", state="coasting")), ledger)

    many = [raw(f"260925-{i:06X}", hit=i) for i in range(MAX_TRACKS)]
    n = MAX_TRACKS
    crowded = {"delay_km": [10.0] * n, "doppler_hz": [1.0] * n, "snr_db": [10.0] * n}
    body = build(tracked(*many), ledger, **crowded)
    assert len(body["tracks"]) == MAX_TRACKS
    assert "dying" not in states(body)

    body = build(tracked(), ledger)
    assert states(body)["dying"] == "deleted"


def test_a_track_the_spec_refuses_costs_that_track_only():
    body = build(tracked(raw(anomaly_types=["Orbit!"]), raw("260925-00001B", hit=2)))

    assert states(body) == {"260925-00001B": "active"}


def test_a_run_the_spec_refuses_sends_no_tracks():
    body = build(tracked(raw(), run="not a run"))

    assert "tracker" not in body and "tracks" not in body


def test_without_a_ledger_a_frame_still_builds():
    """Callers that do not care about lifecycle, such as the probe tools."""
    frame = build_detection_frame(
        poll(), boot_id="28a156bd3f8652f4", seq=1, config_version=7, tracker=tracked(raw())
    )

    assert frame.tracks[0].hit == 1


@pytest.mark.parametrize("field", ["born_timestamp_ms", "avg_snr_db", "shadow_fraction"])
def test_unknown_optionals_are_absent_on_the_wire(field):
    body = build(tracked(dataclasses.replace(raw(), **{field: None})))

    wire_name = {"born_timestamp_ms": "born_t", "avg_snr_db": "avg_snr"}.get(field, field)
    assert wire_name not in body["tracks"][0]
