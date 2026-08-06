"""blah2-api client — the node's only detection source.

We talk to blah2-api, never to blah2 itself. blah2 emits one JSON object per
CPI over a raw TCP socket; blah2-api accumulates it, enriches it with ADS-B
association when that is enabled, and serves the latest one at
``GET /api/detection``. There is no history: it is a latest-value register.

Polling a latest-value register is normally the wrong shape, but the transport
this feeds is explicitly latest-wins with at most one request in flight, so the
two semantics match exactly — and it costs zero changes to blah2 or blah2-api.
We poll faster than the producer (~4 Hz against 2 Hz) to reduce aliasing misses
and dedupe on ``timestamp``. Missed frames are expected and correct.

**No unit conversion happens here.** ``delay`` is kilometres of bistatic range
and ``timestamp`` is epoch milliseconds, because that is what blah2 produces;
the field names say so, and ``wire/`` converts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:3000"

#: How many CPIs may pass without the timestamp advancing before we call blah2
#: wedged. Ten is deliberately slack: the point is to catch a hung detector, not
#: to flap on a single slow CPI.
DEFAULT_STALE_AFTER_CPIS = 10

#: Floor for the staleness window, so an unusually short CPI cannot make the
#: check hair-trigger.
MIN_STALE_AFTER_S = 3.0


class Liveness(StrEnum):
    """What the detection poll tells us about blah2.

    The third state is the whole reason this is derived from the poll rather
    than read from container state: a wedged blah2 has a perfectly healthy
    container, so anything watching the docker socket reports it as up.
    """

    UNKNOWN = "unknown"  #: not polled yet
    DOWN = "down"  #: the poll itself failed
    WEDGED = "wedged"  #: poll succeeds, timestamp is not advancing
    UP = "up"  #: poll succeeds, timestamp is advancing


class MalformedFrame(ValueError):
    """A frame that cannot be trusted onto the wire.

    blah2 builds the three arrays from one loop bounded by ``delay.size()``, so
    they are equal-length by construction — but nothing validates it, and
    ``Detection``'s constructor accepts three vectors of any lengths. A future
    desync would read out of bounds silently rather than throw, so we check at
    the boundary regardless.
    """


@dataclass(frozen=True)
class DetectionPoll:
    """One CPI's detections, in blah2's own units.

    ``adsb`` distinguishes two cases that must not be conflated:

    * ``None`` — blah2-api sent no ``adsb`` key at all, meaning ADS-B
      association is disabled on this node. Stage 2 synthesises ``[None] * n``.
    * ``[]`` — ADS-B is enabled and this frame simply has no detections.
    """

    timestamp_ms: int
    delay_km: list[float]
    doppler_hz: list[float]
    snr_db: list[float]
    adsb: list[dict[str, Any] | None] | None

    @property
    def n_detections(self) -> int:
        return len(self.delay_km)

    @property
    def is_empty(self) -> bool:
        """An empty frame is a normal, meaningful state and is worth sending."""
        return not self.delay_km


def parse_frame(payload: object) -> DetectionPoll:
    """Validate and shape one ``/api/detection`` body.

    Raises:
        MalformedFrame: if the payload is not a frame we can trust.
    """
    if not isinstance(payload, dict):
        raise MalformedFrame(f"expected a JSON object, got {type(payload).__name__}")

    # Verified as an integer by reading Detection.cpp, but accepting an
    # integral float too: a type surprise on a real node should not stop the
    # stream outright, and a fractional millisecond would be the real signal
    # that something changed upstream.
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int | float):
        raise MalformedFrame(f"timestamp must be a number, got {timestamp!r}")
    if isinstance(timestamp, float) and not timestamp.is_integer():
        raise MalformedFrame(f"timestamp must be whole milliseconds, got {timestamp!r}")
    timestamp = int(timestamp)

    arrays: dict[str, list[float]] = {}
    for key in ("delay", "doppler", "snr"):
        value = payload.get(key)
        if not isinstance(value, list):
            raise MalformedFrame(f"{key} must be an array, got {type(value).__name__}")
        for item in value:
            if not isinstance(item, int | float) or isinstance(item, bool):
                raise MalformedFrame(f"{key} contains a non-numeric entry: {item!r}")
        arrays[key] = [float(item) for item in value]

    lengths = {key: len(value) for key, value in arrays.items()}
    if len(set(lengths.values())) != 1:
        raise MalformedFrame(f"parallel arrays disagree in length: {lengths}")
    n = next(iter(lengths.values()))

    adsb = payload.get("adsb")
    if adsb is not None:
        if not isinstance(adsb, list):
            raise MalformedFrame(f"adsb must be an array, got {type(adsb).__name__}")
        if len(adsb) != n:
            raise MalformedFrame(f"adsb has {len(adsb)} entries, expected {n}")
        for item in adsb:
            if item is not None and not isinstance(item, dict):
                raise MalformedFrame(f"adsb entries must be objects or null, got {item!r}")

    return DetectionPoll(
        timestamp_ms=timestamp,
        delay_km=arrays["delay"],
        doppler_hz=arrays["doppler"],
        snr_db=arrays["snr"],
        adsb=adsb,
    )


class Blah2Client:
    """Polls blah2-api, dedupes on timestamp, and tracks liveness.

    ``cpi_s`` is injected rather than read from config here: judging staleness
    needs it, and injecting keeps the derivation next to the data it derives
    from without ``collect.blah2`` importing ``collect.node_config``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        cpi_s: float,
        timeout_s: float = 2.0,
        stale_after_cpis: int = DEFAULT_STALE_AFTER_CPIS,
        session: Any = None,
        clock: Any = monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._stale_after_s = max(stale_after_cpis * cpi_s, MIN_STALE_AFTER_S)
        self._clock = clock

        if session is None:
            import requests

            session = requests.Session()
        self._session = session

        self._last_timestamp_ms: int | None = None
        self._last_change_at: float | None = None
        self._first_ok_at: float | None = None
        self._last_poll_ok: bool | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0

    # ── polling ──────────────────────────────────────────────────────

    def poll_detection(self) -> DetectionPoll | None:
        """Fetch the current frame.

        Returns:
            The frame, or ``None`` if the poll failed, the payload was
            malformed, or the timestamp is one we have already seen. All three
            are ordinary outcomes on this path — the caller publishes what it
            gets and reads :attr:`liveness` separately.
        """
        try:
            response = self._session.get(f"{self._base_url}/api/detection", timeout=self._timeout_s)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - the poll loop must never die
            self._record_failure(f"detection poll failed: {exc}")
            return None

        now = self._clock()
        self._last_poll_ok = True
        self._consecutive_failures = 0
        if self._first_ok_at is None:
            self._first_ok_at = now

        try:
            frame = parse_frame(payload)
        except MalformedFrame as exc:
            # blah2-api answered, so it is up; the data is the problem. Leaving
            # the change clock untouched means persistent garbage eventually
            # reads as wedged, which is the honest description.
            self._last_error = f"malformed frame: {exc}"
            log.warning("discarding malformed detection frame: %s", exc)
            return None

        if frame.timestamp_ms == self._last_timestamp_ms:
            return None  # polled twice inside one CPI, which is expected

        self._last_timestamp_ms = frame.timestamp_ms
        self._last_change_at = now
        self._last_error = None
        return frame

    def get_json(self, path: str) -> Any | None:
        """Best-effort GET of any blah2-api endpoint. ``None`` on any failure.

        Diagnostic paths hang off this rather than getting their own methods,
        since their response shapes are not verified against a running node.
        """
        try:
            response = self._session.get(
                f"{self._base_url}/{path.lstrip('/')}", timeout=self._timeout_s
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - diagnostics must not break a beat
            log.debug("GET %s failed: %s", path, exc)
            return None

    def timing(self) -> Any | None:
        """Per-stage processing time. A ``cpi`` total over ``cpi_s * 1000`` ms
        means the ring buffer is dropping samples, which nothing else reveals.
        """
        return self.get_json("/api/timing")

    def close(self) -> None:
        closer = getattr(self._session, "close", None)
        if closer is not None:
            closer()

    # ── derived health ───────────────────────────────────────────────

    @property
    def liveness(self) -> Liveness:
        if self._last_poll_ok is None:
            return Liveness.UNKNOWN
        if not self._last_poll_ok:
            return Liveness.DOWN

        # Falling back to the first successful poll gives the same grace period
        # to a node that has answered but never yet produced a usable frame.
        reference = self._last_change_at if self._last_change_at is not None else self._first_ok_at
        assert reference is not None  # implied by _last_poll_ok being True
        if self._clock() - reference > self._stale_after_s:
            return Liveness.WEDGED
        return Liveness.UP

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_timestamp_ms(self) -> int | None:
        return self._last_timestamp_ms

    def _record_failure(self, message: str) -> None:
        self._last_poll_ok = False
        self._consecutive_failures += 1
        self._last_error = message
        log.debug("%s", message)
