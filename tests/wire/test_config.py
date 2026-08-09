import dataclasses

import pytest

from retina_telemetry.collect.node_config import NodeConfigRaw
from retina_telemetry.wire.config import IncompleteConfig, build_node_config

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
    beam_width_deg=60.0,  # Q1 — not on a real node yet
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


# ── the Q1 blocker ───────────────────────────────────────────────────


def test_missing_beam_width_raises():
    """The intended failure. Stage 1 reads it as optional because raising there
    would make every node unreadable; this is the layer that knows the spec
    requires it."""
    unconfigured = dataclasses.replace(OWL, beam_width_deg=None)

    with pytest.raises(IncompleteConfig, match="Q1"):
        build_node_config(unconfigured)


def test_the_error_says_what_to_do_about_it():
    unconfigured = dataclasses.replace(OWL, beam_width_deg=None)

    with pytest.raises(IncompleteConfig) as caught:
        build_node_config(unconfigured)

    assert "not configured on this node" in str(caught.value)


def test_no_beam_width_is_ever_invented():
    """A guessed beam width is worse than a node that will not register: the
    first is wrong data the server cannot detect."""
    unconfigured = dataclasses.replace(OWL, beam_width_deg=None)

    with pytest.raises(IncompleteConfig):
        build_node_config(unconfigured)


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
