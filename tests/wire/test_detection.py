import json

import pydantic
import pytest

from retina_telemetry.collect.blah2 import DetectionPoll
from retina_telemetry.wire.detection import build_detection_frame
from retina_telemetry.wire.serialise import to_wire
from tests.fakes.blah2_api import ASSOCIATION


def poll(**overrides) -> DetectionPoll:
    return DetectionPoll(
        **{
            "timestamp_ms": 1786014064679,
            "delay_km": [12.4, 30.1],
            "doppler_hz": [-118.0, 44.5],
            "snr_db": [14.2, 9.8],
            "adsb": None,
            **overrides,
        }
    )


def test_every_field_traced_to_its_source():
    frame = build_detection_frame(poll(), boot_id="28a156bd3f8652f4", seq=918273, config_version=7)

    assert frame.t == 1786014064.679  # timestamp_ms ÷ 1000
    assert frame.seq == 918273  # argument
    assert frame.config_version == 7  # argument
    assert frame.delay == [41.362, 100.403]  # delay_km × 3.335641
    assert frame.doppler == [-118.0, 44.5]  # unchanged
    assert frame.snr == [14.2, 9.8]  # unchanged
    assert frame.adsb is None  # ADS-B off, so no association column at all


def test_doppler_and_snr_are_not_converted():
    """Both are already in the spec's units. A conversion here would be a
    silent corruption with no boundary to catch it."""
    frame = build_detection_frame(
        poll(doppler_hz=[-1.0], snr_db=[2.0], delay_km=[3.0]),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    assert frame.doppler == [-1.0]
    assert frame.snr == [2.0]


# ── the association column ───────────────────────────────────────────


def test_the_deprecated_hex_column_is_never_sent():
    """Contract 1.5.0 deprecated ``adsb_hex``: the tag says which aircraft was
    matched as well as where it was, so sending both put every match on the
    wire twice and nothing on the server read the hex column."""
    frame = build_detection_frame(
        poll(adsb=[ASSOCIATION, None]), boot_id="28a156bd3f8652f4", seq=1, config_version=1
    )

    assert "adsb_hex" not in to_wire(frame)


def test_malformed_association_costs_one_entry_not_the_frame():
    """A missing hex should not throw away two good detections."""
    frame = build_detection_frame(
        poll(adsb=[{"lat": 51.5, "lon": -0.1}, ASSOCIATION]),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    tags = to_wire(frame)["adsb"]
    assert tags[0] is None
    assert tags[1]["hex"] == "4ca1f2"


# ── empty frames ─────────────────────────────────────────────────────


def test_empty_frame_is_a_valid_payload():
    """41 of 101 frames on Owl were empty. The spec wants them sent."""
    frame = build_detection_frame(
        poll(delay_km=[], doppler_hz=[], snr_db=[], adsb=None),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    assert frame.delay == []
    assert frame.adsb is None  # association off
    assert frame.model_dump()["delay"] == []


def test_empty_frame_with_adsb_enabled():
    frame = build_detection_frame(
        poll(delay_km=[], doppler_hz=[], snr_db=[], adsb=[]),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    assert frame.adsb == []


# ── the arrays stay parallel ─────────────────────────────────────────


def test_all_four_arrays_are_the_same_length():
    for n in (0, 1, 5):
        frame = build_detection_frame(
            poll(
                delay_km=[1.0] * n,
                doppler_hz=[2.0] * n,
                snr_db=[3.0] * n,
                adsb=[ASSOCIATION] * n,
            ),
            boot_id="28a156bd3f8652f4",
            seq=1,
            config_version=1,
        )
        lengths = {
            len(frame.delay),
            len(frame.doppler),
            len(frame.snr),
            len(frame.adsb),
        }
        assert lengths == {n}


def test_source_lists_are_not_aliased():
    """The frame must not change if the poll's lists are mutated afterwards."""
    source = poll()
    frame = build_detection_frame(source, seq=1, boot_id="28a156bd3f8652f4", config_version=1)
    source.doppler_hz.append(999.0)

    assert frame.doppler == [-118.0, 44.5]


# ── spec constraints ─────────────────────────────────────────────────


def test_config_version_must_be_at_least_one():
    """The generated model carries the spec's `minimum: 1`, so a zero cannot
    be sent even by accident."""
    with pytest.raises(pydantic.ValidationError):
        build_detection_frame(poll(), boot_id="28a156bd3f8652f4", seq=1, config_version=0)


# ── the spec's hex pattern ───────────────────────────────────────────


def test_a_hex_that_is_not_icao_becomes_null():
    """One malformed association would otherwise cost the whole frame, taking
    every other detection with it."""
    frame = build_detection_frame(
        poll(adsb=[{"hex": "NOTHEX"}, ASSOCIATION]),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    tags = to_wire(frame)["adsb"]
    assert tags[0] is None
    assert tags[1]["hex"] == "4ca1f2"


def test_uppercase_hex_becomes_null():
    """The spec's pattern is lowercase only, and blah2-api emits lowercase — but
    a frame is too expensive to lose over the difference."""
    frame = build_detection_frame(
        poll(adsb=[{"hex": "4CA1F2"}]), boot_id="28a156bd3f8652f4", seq=1, config_version=1
    )

    assert to_wire(frame)["adsb"] == [None]


def test_arrays_are_capped_at_the_spec_bound():
    """maxItems is 512. Single figures in practice, so this only fires on
    something pathological — and a truncated frame beats no frame."""
    n = 600
    frame = build_detection_frame(
        poll(
            delay_km=[1.0] * n,
            doppler_hz=[2.0] * n,
            snr_db=[3.0] * n,
            adsb=[ASSOCIATION] * n,
        ),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    payload = to_wire(frame)
    assert len(payload["delay"]) == 512
    assert len({len(payload[k]) for k in ("delay", "doppler", "snr", "adsb")}) == 1


# ── values that cannot survive JSON ──────────────────────────────────


def test_a_non_finite_value_costs_one_detection_not_the_frame():
    """`inf` and `nan` serialise as the bare tokens Infinity and NaN, which are
    not valid JSON — a strict parser rejects the whole body. Reachable rather
    than theoretical: snr is 10*log10(|x|) - noisePower, so a zero-magnitude
    detection gives -inf."""
    frame = build_detection_frame(
        poll(delay_km=[12.4, float("inf")], doppler_hz=[-118.0, 1.0], snr_db=[14.2, 2.0]),
        seq=1,
        boot_id="28a156bd3f8652f4",
        config_version=1,
    )

    payload = to_wire(frame)

    assert payload["delay"] == [41.362]
    assert len({len(payload[k]) for k in ("delay", "doppler", "snr")}) == 1


def test_every_frame_survives_a_strict_json_parser():
    """The property that actually matters. json.dumps emits Infinity happily;
    it is the receiving parser that refuses."""
    frame = build_detection_frame(
        poll(
            delay_km=[float("inf"), 12.4, float("-inf")],
            doppler_hz=[1.0, -118.0, 3.0],
            snr_db=[float("nan"), 14.2, 3.0],
        ),
        seq=1,
        boot_id="28a156bd3f8652f4",
        config_version=1,
    )

    body = json.dumps(to_wire(frame))

    def refuse(token):
        raise AssertionError(f"emitted the non-JSON token {token}")

    json.loads(body, parse_constant=refuse)


def test_a_wholly_non_finite_frame_becomes_an_empty_one():
    """Empty is a valid frame and worth sending — the detector was running."""
    frame = build_detection_frame(
        poll(delay_km=[float("inf")], doppler_hz=[float("nan")], snr_db=[1.0]),
        seq=1,
        boot_id="28a156bd3f8652f4",
        config_version=1,
    )

    assert to_wire(frame)["delay"] == []


class TestPositionTags:
    """``adsb`` — the association said again with the position it was made at."""

    def test_an_association_carries_its_position(self):
        frame = build_detection_frame(
            poll(adsb=[ASSOCIATION, None]), boot_id="28a156bd3f8652f4", seq=1, config_version=7
        )

        tag, none = frame.adsb
        assert none is None
        assert (tag.hex, tag.lat, tag.lon, tag.alt) == ("4ca1f2", 51.5, -0.1, 11000.0)
        assert (tag.expected_delay, tag.expected_doppler) == (12.3, -117.5)
        assert (tag.delay_residual, tag.doppler_residual) == (0.1, -0.5)
        assert tag.gs is None and tag.track is None  # blah2-api did not report them

    def test_the_column_is_omitted_when_association_is_off(self):
        frame = build_detection_frame(poll(), boot_id="28a156bd3f8652f4", seq=1, config_version=7)

        assert frame.adsb is None
        # Neither column. That is how a node says it matched nothing.
        assert "adsb" not in to_wire(frame)
        assert "adsb_hex" not in to_wire(frame)

    def test_the_wire_shape_matches_the_documented_frame(self):
        frame = build_detection_frame(
            poll(adsb=[ASSOCIATION, None]), boot_id="28a156bd3f8652f4", seq=1, config_version=7
        )

        assert to_wire(frame)["adsb"] == [
            {
                "hex": "4ca1f2",
                "lat": 51.5,
                "lon": -0.1,
                "alt": 11000.0,
                # to_wire prunes optional nulls at the top level only; inside a
                # tag they ride along, and the contract admits them.
                "gs": None,
                "track": None,
                "expected_delay": 12.3,
                "expected_doppler": -117.5,
                "delay_residual": 0.1,
                "doppler_residual": -0.5,
            },
            None,
        ]

    def test_an_association_without_a_position_is_dropped(self):
        """What the deprecation costs, and the server author signed it off: a
        match with no usable position used to travel as a bare hex and now
        travels as null. Nothing on the server reads a bare hex."""
        frame = build_detection_frame(
            poll(adsb=[{"hex": "4ca1f2"}, {"hex": "abc123", "lat": 51.5, "lon": None}]),
            boot_id="28a156bd3f8652f4",
            seq=1,
            config_version=7,
        )

        assert frame.adsb == [None, None]

    @pytest.mark.parametrize(
        "bad", [{"lat": 95.0}, {"lon": "-0.1"}, {"lat": float("nan")}, {"lat": True}]
    )
    def test_a_position_the_spec_refuses_costs_that_tag_only(self, bad):
        frame = build_detection_frame(
            poll(adsb=[ASSOCIATION | bad, ASSOCIATION]),
            boot_id="28a156bd3f8652f4",
            seq=1,
            config_version=7,
        )

        assert frame.adsb[0] is None
        assert frame.adsb[1] is not None

    def test_a_dropped_detection_takes_its_tag_with_it(self):
        frame = build_detection_frame(
            poll(
                delay_km=[float("nan"), 30.1], adsb=[ASSOCIATION, ASSOCIATION | {"hex": "abc123"}]
            ),
            boot_id="28a156bd3f8652f4",
            seq=1,
            config_version=7,
        )

        assert [t.hex for t in frame.adsb] == ["abc123"]
