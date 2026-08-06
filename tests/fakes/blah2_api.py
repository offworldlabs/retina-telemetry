"""A scriptable stand-in for blah2-api, plus captured response shapes.

The payloads here mirror what a real node produces, including the two shapes
that are easy to forget: an empty frame (detector running, nothing detected)
and a frame with no ``adsb`` key at all (ADS-B association disabled).
"""

from __future__ import annotations

from typing import Any


class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    """Serves scripted responses in order, repeating the last one forever.

    An entry may be a payload (served as ``200``), a :class:`FakeResponse`, or
    an ``Exception`` instance, which is raised from ``get`` to stand in for a
    connection failure.
    """

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses) or [None]
        self.calls: list[str] = []
        self.closed = False

    def get(self, url: str, timeout: float | None = None) -> FakeResponse:
        self.calls.append(url)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)

    def close(self) -> None:
        self.closed = True


class FakeClock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def frame(
    timestamp_ms: int = 1753900000123,
    *,
    delay: list[float] | None = None,
    doppler: list[float] | None = None,
    snr: list[float] | None = None,
    adsb: list[dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """A ``/api/detection`` body. ``adsb`` is omitted entirely unless given,
    which is what blah2-api does when ``truth.adsb.enabled`` is false."""
    payload: dict[str, Any] = {
        "timestamp": timestamp_ms,
        "delay": [12.4, 30.1] if delay is None else delay,
        "doppler": [-118.0, 44.5] if doppler is None else doppler,
        "snr": [14.2, 9.8] if snr is None else snr,
    }
    if adsb is not None:
        payload["adsb"] = adsb
    return payload


def empty_frame(timestamp_ms: int = 1753900000123) -> dict[str, Any]:
    """Detector running, nothing detected. A normal state, and worth sending."""
    return frame(timestamp_ms, delay=[], doppler=[], snr=[])


#: One association as blah2-api builds it — an object, not a hex string.
ASSOCIATION = {
    "hex": "4ca1f2",
    "lat": 51.5,
    "lon": -0.1,
    "alt": 11000,
    "expected_delay": 12.3,
    "expected_doppler": -117.5,
    "delay_residual": 0.1,
    "doppler_residual": -0.5,
}
