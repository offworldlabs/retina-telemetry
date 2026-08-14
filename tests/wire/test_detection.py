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
    assert frame.adsb_hex == [None, None]  # synthesised, ADS-B off


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


# ── adsb_hex ─────────────────────────────────────────────────────────


def test_absent_adsb_synthesises_nulls_of_the_right_length():
    """All four arrays must be equal-length, so a disabled ADS-B produces
    nulls rather than an omitted field."""
    frame = build_detection_frame(
        poll(adsb=None), boot_id="28a156bd3f8652f4", seq=1, config_version=1
    )

    assert frame.adsb_hex == [None, None]
    assert len(frame.adsb_hex) == len(frame.delay)


def test_associations_reduced_to_hex():
    """blah2-api sends objects; the spec wants the ICAO hex only.

    Asserted on the serialised payload rather than the model attribute: the
    spec's ``^[0-9a-f]{6}$`` on the array items makes the generator wrap them in
    a RootModel, which is transparent through serialisation and visible only to
    direct attribute access.
    """
    frame = build_detection_frame(
        poll(adsb=[ASSOCIATION, None]), boot_id="28a156bd3f8652f4", seq=1, config_version=1
    )

    assert to_wire(frame)["adsb_hex"] == ["4ca1f2", None]


def test_malformed_association_costs_one_entry_not_the_frame():
    """A missing hex should not throw away two good detections."""
    frame = build_detection_frame(
        poll(adsb=[{"lat": 51.5, "lon": -0.1}, ASSOCIATION]),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    assert to_wire(frame)["adsb_hex"] == [None, "4ca1f2"]


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
    assert frame.adsb_hex == []
    assert frame.model_dump()["delay"] == []


def test_empty_frame_with_adsb_enabled():
    frame = build_detection_frame(
        poll(delay_km=[], doppler_hz=[], snr_db=[], adsb=[]),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    assert frame.adsb_hex == []


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
            len(frame.adsb_hex),
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

    assert to_wire(frame)["adsb_hex"] == [None, "4ca1f2"]


def test_uppercase_hex_becomes_null():
    """The spec's pattern is lowercase only, and blah2-api emits lowercase — but
    a frame is too expensive to lose over the difference."""
    frame = build_detection_frame(
        poll(adsb=[{"hex": "4CA1F2"}]), boot_id="28a156bd3f8652f4", seq=1, config_version=1
    )

    assert to_wire(frame)["adsb_hex"] == [None]


def test_arrays_are_capped_at_the_spec_bound():
    """maxItems is 512. Single figures in practice, so this only fires on
    something pathological — and a truncated frame beats no frame."""
    n = 600
    frame = build_detection_frame(
        poll(delay_km=[1.0] * n, doppler_hz=[2.0] * n, snr_db=[3.0] * n, adsb=None),
        boot_id="28a156bd3f8652f4",
        seq=1,
        config_version=1,
    )

    payload = to_wire(frame)
    assert len(payload["delay"]) == 512
    assert len({len(payload[k]) for k in ("delay", "doppler", "snr", "adsb_hex")}) == 1


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
    assert len({len(payload[k]) for k in ("delay", "doppler", "snr", "adsb_hex")}) == 1


def test_a_dropped_detection_takes_its_adsb_entry_with_it():
    """All four arrays must stay parallel, so the index goes from every one."""
    frame = build_detection_frame(
        poll(
            delay_km=[12.4, 30.1],
            doppler_hz=[float("nan"), 44.5],
            snr_db=[14.2, 9.8],
            adsb=[ASSOCIATION, None],
        ),
        seq=1,
        boot_id="28a156bd3f8652f4",
        config_version=1,
    )

    payload = to_wire(frame)

    assert payload["doppler"] == [44.5]
    assert payload["adsb_hex"] == [None]


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
