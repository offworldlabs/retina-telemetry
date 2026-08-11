import dataclasses

import pytest

from retina_telemetry.collect.node_config import NodeConfigRaw
from retina_telemetry.wire.config import build_node_config

# Owl's real values, from the live probe.
OWL = NodeConfigRaw(
    rx_lat=51.4769,
    rx_lon=-0.0005,
    rx_alt_m=48.0,
    tx_lat=51.4244,
    tx_lon=-0.0753,
    tx_alt_m=219.0,
    tx_name="Crystal Palace",
    fc_hz=503000000.0,
    fs_hz=2000000.0,
    delay_max_bins=400,
    beam_width_deg=60.0,  # not written on a real node; see Q1
    beam_azimuth_deg=None,
)


def test_every_field_traced_to_its_source():
    wire = build_node_config(OWL)

    assert wire.rx_lat == 51.4769  # unchanged
    assert wire.rx_lon == -0.0005  # unchanged
    assert wire.rx_alt_ft == 157.5  # rx_alt_m × 3.28084
    assert wire.tx_lat == 51.4244  # unchanged
    assert wire.tx_lon == -0.0753  # unchanged
    assert wire.tx_alt_ft == 718.5  # tx_alt_m × 3.28084
    assert wire.tx_callsign == "Crystal Palace"  # tx_name
    assert wire.fc_hz == 503000000.0  # unchanged
    assert wire.fs_hz == 2000000.0  # unchanged
    assert wire.max_range_km == 59.96  # derived from bins and fs
    assert wire.beam_width_deg == 60.0
    assert wire.beam_azimuth_deg is None


def test_altitudes_are_converted_not_passed_through():
    """The one non-SI field in the spec, and the easiest to forget."""
    wire = build_node_config(OWL)

    assert wire.rx_alt_ft != OWL.rx_alt_m
    assert wire.tx_alt_ft != OWL.tx_alt_m


def test_coordinates_are_not_converted():
    """Degrees on both sides. A conversion here would move the node."""
    wire = build_node_config(OWL)

    assert (wire.rx_lat, wire.rx_lon) == (OWL.rx_lat, OWL.rx_lon)


def test_max_range_is_derived_not_read():
    """delay_max_bins never reaches the wire; the km figure is computed so it
    cannot disagree with what blah2 actually does."""
    wire = build_node_config(OWL)

    assert wire.max_range_km == 59.96
    assert not hasattr(wire, "delay_max_bins")


def test_tx_callsign_carries_a_display_name():
    """location.tx.name is free text the operator typed, not a regulatory
    callsign. Q5 asks which the server wants."""
    assert build_node_config(OWL).tx_callsign == "Crystal Palace"


# ── an uncharacterised antenna: the normal case ──────────────────────
#
# retina-gui is not collecting beam geometry from owners for the foreseeable
# future, so every node in the fleet takes these paths. They are the default
# behaviour, not an edge case.


def test_an_unset_beam_width_is_omitted_not_defaulted():
    """Nothing is substituted. A value the node did not give us must never reach
    the server — the same discipline as the consent records."""
    unconfigured = dataclasses.replace(OWL, beam_width_deg=None)

    payload = build_node_config(unconfigured).model_dump(mode="json", exclude_none=True)

    assert "beam_width_deg" not in payload


def test_an_uncharacterised_antenna_omits_both_keys():
    bare = dataclasses.replace(OWL, beam_width_deg=None, beam_azimuth_deg=None)

    payload = build_node_config(bare).model_dump(mode="json", exclude_none=True)

    assert "beam_width_deg" not in payload
    assert "beam_azimuth_deg" not in payload
    assert payload["rx_lat"] == 51.4769  # the rest of the config is intact


def test_no_beam_width_is_ever_invented():
    """Two earlier designs are superseded: raising, and defaulting to 360. Both
    are recorded in Q1. Neither should come back by accident."""
    payload = build_node_config(dataclasses.replace(OWL, beam_width_deg=None)).model_dump(
        mode="json", exclude_none=True
    )

    assert payload.get("beam_width_deg") is None


def test_a_configured_width_is_sent_verbatim():
    for width in (60.0, 360.0):
        payload = build_node_config(dataclasses.replace(OWL, beam_width_deg=width)).model_dump(
            mode="json", exclude_none=True
        )
        assert payload["beam_width_deg"] == width


def test_a_known_azimuth_survives_an_unknown_width():
    """The two fields are independent. An operator who knows where the antenna
    points but not how wide the beam is has told us something true."""
    partial = dataclasses.replace(OWL, beam_width_deg=None, beam_azimuth_deg=90.0)

    payload = build_node_config(partial).model_dump(mode="json", exclude_none=True)

    assert "beam_width_deg" not in payload
    assert payload["beam_azimuth_deg"] == 90.0


def test_absent_azimuth_is_valid_and_means_omnidirectional():
    """Unlike width, a missing azimuth is not an error — the spec asks for null
    rather than 0.0 for broadside."""
    wire = build_node_config(dataclasses.replace(OWL, beam_azimuth_deg=None))

    assert wire.beam_azimuth_deg is None
    assert "beam_azimuth_deg" in wire.model_dump()


def test_azimuth_is_passed_through_when_set():
    wire = build_node_config(dataclasses.replace(OWL, beam_azimuth_deg=135.5))

    assert wire.beam_azimuth_deg == 135.5


def test_zero_azimuth_is_not_turned_into_none():
    """0.0 means due north, not omnidirectional. Conflating them would silently
    reorient a directional node."""
    wire = build_node_config(dataclasses.replace(OWL, beam_azimuth_deg=0.0))

    assert wire.beam_azimuth_deg == 0.0


def test_invalid_sample_rate_surfaces_from_the_derivation():
    with pytest.raises(ValueError, match="fs_hz must be positive"):
        build_node_config(dataclasses.replace(OWL, fs_hz=0.0))
