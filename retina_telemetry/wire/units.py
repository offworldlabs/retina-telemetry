"""Every unit conversion in the service, in one place.

Stage 1 is deliberately faithful to source units, so this module is the only
thing standing between what blah2 produces and what the server is told. A
silent error here is invisible on the wire — a transmitter reported ~500 ft low
looks exactly like a transmitter that is 500 ft lower — which is why these get
the most direct tests in the repo.

Each function is named for the direction it converts, so a call site reads as
an assertion about units: ``km_to_us(poll.delay_km)`` is obviously right, and
``km_to_us(poll.doppler_hz)`` is obviously wrong.
"""

from __future__ import annotations

#: Microseconds of bistatic delay per kilometre of bistatic range.
#: 1 km ÷ 299 792.458 km/s = 3.335641 µs.
KM_TO_US = 3.335641

#: Feet per metre. Node config stores altitudes in metres; the spec wants feet.
M_TO_FT = 3.28084

#: Metres per second, for the max_range_km derivation.
SPEED_OF_LIGHT_M_S = 299_792_458.0

#: blah2 quantises delay to 2 dp in km — 0.01 km ≈ 0.0334 µs — so anything finer
#: than about 3 dp in microseconds is fabricated precision. Rounding here also
#: keeps float noise (41.361948400000004) off the wire. See Q4, which asks the
#: server author whether they would rather receive kilometres for this reason.
DELAY_DECIMALS = 3

#: One CPI is 0.5 s and the timestamp is integer milliseconds, so three decimals
#: is exact rather than a choice.
SECONDS_DECIMALS = 3

ALTITUDE_DECIMALS = 1
RANGE_DECIMALS = 2


def ms_to_s(timestamp_ms: int) -> float:
    """Epoch milliseconds → epoch float seconds.

    blah2's ``timestamp``, which is the **end** of the capture window rather
    than its start or middle (Q3 asks the server author to state which edge the
    spec means).
    """
    return round(timestamp_ms / 1000.0, SECONDS_DECIMALS)


def km_to_us(delay_km: list[float]) -> list[float]:
    """Bistatic range in kilometres → bistatic delay in microseconds."""
    return [round(value * KM_TO_US, DELAY_DECIMALS) for value in delay_km]


def tolerance_km_to_us(tolerance_km: float) -> float:
    """The ADS-B association gate, kilometres → microseconds.

    Same constant as :func:`km_to_us` and for the same reason: blah2-api
    compares the gate against a bistatic range in kilometres
    (``api/bistatic.js:67``), while the spec wants it in the unit
    ``DetectionFrame.delay`` travels in. A separate function because it takes a
    scalar, and because a call site reading ``tolerance_km_to_us`` says what is
    being converted where ``km_to_us`` would not.
    """
    return round(tolerance_km * KM_TO_US, DELAY_DECIMALS)


def m_to_ft(altitude_m: float) -> float:
    """Metres → feet.

    Everything else in the spec is SI, so this one conversion is the odd one
    out and the easiest to forget. Q4 asks why.
    """
    return round(altitude_m * M_TO_FT, ALTITUDE_DECIMALS)


def max_range_km(delay_max_bins: int, fs_hz: float) -> float:
    """Derive the maximum range of interest from the ambiguity map's extent.

    ``delayMax`` is a bin count, and one bin is one sample period of bistatic
    range, so the range is ``bins × c / fs``. Derived rather than stored so it
    can never disagree with what blah2 actually computes — 400 bins at 2 MHz
    gives ~60 km. See Q6.

    Raises:
        ValueError: if ``fs_hz`` is zero or negative, which would otherwise
            produce an infinity that pydantic will happily serialise.
    """
    if fs_hz <= 0:
        raise ValueError(f"fs_hz must be positive, got {fs_hz}")
    return round(delay_max_bins * SPEED_OF_LIGHT_M_S / fs_hz / 1000.0, RANGE_DECIMALS)
