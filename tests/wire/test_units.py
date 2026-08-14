"""The most direct tests in the repo.

A wrong conversion here is invisible on the wire: a transmitter reported 500 ft
low reads exactly like a transmitter that is 500 ft lower.
"""

import pytest

from retina_telemetry.wire.units import (
    KM_TO_US,
    M_TO_FT,
    km_to_us,
    m_to_ft,
    max_range_km,
    ms_to_s,
)

# ── kilometres → microseconds ────────────────────────────────────────


def test_one_km_of_bistatic_range():
    """1 km ÷ 299 792.458 km/s = 3.335641 µs."""
    assert km_to_us([1.0]) == [3.336]


def test_converts_a_real_frame():
    """The example from data-sources.md §1."""
    assert km_to_us([12.4, 30.1]) == [41.362, 100.403]


def test_delay_is_multiplied_not_divided():
    """The failure mode worth naming: dividing gives a plausible small number."""
    converted = km_to_us([100.0])[0]

    assert converted > 100.0
    assert converted == pytest.approx(100 * KM_TO_US, abs=0.001)


def test_empty_frame_converts_to_empty():
    assert km_to_us([]) == []


def test_rounded_to_source_precision():
    """blah2 quantises to 2 dp in km ≈ 0.0334 µs, so anything finer is invented
    — and full float precision puts 41.361948400000004 on the wire."""
    assert km_to_us([12.4]) == [41.362]
    assert all(len(str(v).split(".")[1]) <= 3 for v in km_to_us([1.1, 2.22, 3.333]))


# ── milliseconds → seconds ───────────────────────────────────────────


def test_epoch_ms_to_epoch_s():
    """A real timestamp observed on Owl."""
    assert ms_to_s(1786014064679) == 1786014064.679


def test_seconds_are_not_integers():
    assert isinstance(ms_to_s(1786014064000), float)


def test_divided_not_multiplied():
    """A timestamp in the wrong direction lands ~55000 years out, which the
    server would notice — but the reverse error would not be caught here."""
    assert ms_to_s(1786014064679) < 2_000_000_000


# ── metres → feet ────────────────────────────────────────────────────


def test_receiver_altitude_from_owl():
    """Owl's rx altitude is 48 m."""
    assert m_to_ft(48.0) == 157.5


def test_transmitter_altitude_from_owl():
    """Crystal Palace at 219 m."""
    assert m_to_ft(219.0) == 718.5


def test_feet_are_larger_than_metres():
    """The silent failure: metres read as feet puts a transmitter ~500 ft low
    with no error anywhere."""
    assert m_to_ft(100.0) > 100.0
    assert m_to_ft(100.0) == pytest.approx(100 * M_TO_FT, abs=0.05)


def test_sea_level_is_unchanged():
    assert m_to_ft(0.0) == 0.0


def test_below_sea_level_stays_negative():
    assert m_to_ft(-10.0) < 0


# ── max range derivation ─────────────────────────────────────────────


def test_derived_from_owls_actual_settings():
    """400 bins at 2 MHz ≈ 60 km, the figure in data-sources.md §4."""
    assert max_range_km(400, 2_000_000) == 59.96


def test_scales_with_bins():
    assert max_range_km(800, 2_000_000) == pytest.approx(2 * 59.96, abs=0.01)


def test_inversely_proportional_to_sample_rate():
    """Doubling fs halves the range each bin represents."""
    assert max_range_km(400, 4_000_000) == pytest.approx(59.96 / 2, abs=0.01)


def test_zero_sample_rate_raises_rather_than_returning_infinity():
    """pydantic would happily serialise an inf, and the server would not."""
    with pytest.raises(ValueError, match="fs_hz must be positive"):
        max_range_km(400, 0)


def test_negative_sample_rate_raises():
    with pytest.raises(ValueError, match="fs_hz must be positive"):
        max_range_km(400, -2_000_000)
