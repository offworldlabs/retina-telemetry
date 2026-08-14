"""blah2-api client — the node's only detection source.

We talk to blah2-api, never to blah2 itself. blah2 emits one JSON object per
CPI over a raw TCP socket; blah2-api accumulates it, enriches it with ADS-B
association when that is enabled, and serves the latest one at
``GET /api/detection``. There is no history: it is a latest-value register.

Polling a latest-value register is normally the wrong shape, but the transport
this feeds is explicitly latest-wins with at most one request in flight, so the
two semantics match exactly — and it costs zero changes to blah2 or blah2-api.
We poll faster than the producer to reduce aliasing misses and dedupe on
``timestamp``. Missed frames are expected and correct.

**No unit conversion happens here.** ``delay`` is kilometres of bistatic range
and ``timestamp`` is epoch milliseconds, because that is what blah2 produces;
the field names say so, and ``wire/`` converts.

**No spec vocabulary here either.** This module reports whether the poll worked;
turning that into ``NodeHealth.blah2``'s ``"up"`` or ``"down"`` is stage 2's job.

Scope is the spec and nothing else. blah2-api also serves ``/api/timing`` — whose
``cpi`` total is the only view of ring-buffer loss — and the capture status
endpoints, and neither has a field to go in.

*Deferred, not rejected:* a third health value for a blah2 that answers but whose
timestamp has stopped advancing. The fleet's own watchdog
(``/etc/cron.d/blah2-rspduo-watchdog``) treats 60 s of staleness as broken and
restarts the stack, so the condition is already detected and fixed elsewhere; and
the server derives wedged-ness from its own record of frame arrivals rather than
from this field. Reinstating it means tracking the last timestamp *change* against
a monotonic clock — not wall clock, which the spec says not to trust — plus reading
``/data/retina-gui/mode.txt``, since blah2 is deliberately stopped in ``spectrum``
and ``sdrconnect`` modes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:3000"


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

    * ``None`` — blah2-api sent no ``adsb`` key at all. The enrichment is gated
      entirely on ``truth.adsb.enabled`` (``api/server.js``), so an absent key
      means ADS-B association is off on this node. Stage 2 synthesises
      ``[None] * n`` for the wire, and reports ``NodeHealth.adsb`` as absent.
    * ``[]`` — ADS-B is on and this frame simply has no detections.

    Because key presence tracks the config flag exactly, nothing needs to read
    ``truth.adsb.enabled`` to interpret this.
    """

    timestamp_ms: int
    delay_km: list[float]
    doppler_hz: list[float]
    snr_db: list[float]
    adsb: list[dict[str, Any] | None] | None

    @property
    def n_detections(self) -> int:
        return len(self.delay_km)


def parse_frame(payload: object) -> DetectionPoll:
    """Validate and shape one ``/api/detection`` body.

    Raises:
        MalformedFrame: if the payload is not a frame we can trust.
    """
    if not isinstance(payload, dict):
        raise MalformedFrame(f"expected a JSON object, got {type(payload).__name__}")

    # Verified as an integer by reading Detection.cpp and confirmed on a live
    # node, but accepting an integral float too: a type surprise should not stop
    # the stream outright, and a fractional millisecond would be the real signal
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
    """Polls blah2-api and dedupes on timestamp.

    Has no time-dependent behaviour: every answer it gives is about the most
    recent poll.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: float = 2.0,
        session: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

        if session is None:
            import requests

            session = requests.Session()
        self._session = session

        self._last_timestamp_ms: int | None = None
        self._last_poll_ok: bool | None = None
        self._last_adsb_present: bool | None = None
        self._last_error: str | None = None

    def poll_detection(self) -> DetectionPoll | None:
        """Fetch the current frame.

        Returns:
            The frame, or ``None`` if the poll failed, the payload was
            malformed, or the timestamp is one we have already seen. All three
            are ordinary outcomes on this path — the caller publishes what it
            gets and reads :attr:`last_poll_ok` separately.
        """
        try:
            response = self._session.get(f"{self._base_url}/api/detection", timeout=self._timeout_s)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 - the poll loop must never die
            self._last_poll_ok = False
            self._last_error = f"detection poll failed: {exc}"
            log.debug("%s", self._last_error)
            return None

        self._last_poll_ok = True

        try:
            frame = parse_frame(payload)
        except MalformedFrame as exc:
            # blah2-api answered, so it is reachable; the data is the problem.
            self._last_error = f"malformed frame: {exc}"
            log.warning("discarding malformed detection frame: %s", exc)
            return None

        if frame.timestamp_ms == self._last_timestamp_ms:
            return None  # polled twice inside one CPI, which is expected

        self._last_timestamp_ms = frame.timestamp_ms
        # Whether blah2-api enriched this frame with associations. It gates the
        # whole `adsb` key on `truth.adsb.enabled`, so the key's presence *is*
        # the configuration flag — which makes this the only way the heartbeat
        # can report NodeHealth.adsb at all.
        self._last_adsb_present = frame.adsb is not None
        self._last_error = None
        return frame

    def close(self) -> None:
        closer = getattr(self._session, "close", None)
        if closer is not None:
            closer()

    @property
    def has_produced(self) -> bool:
        """Whether any frame has ever been polled.

        Collected because a required field depends on it: the spec's `starting`
        state means "before the radar has produced anything", which is a
        different thing from a radar that produced and stopped, and only this
        distinguishes them.
        """
        return self._last_timestamp_ms is not None

    @property
    def last_poll_ok(self) -> bool | None:
        """Whether the most recent poll reached blah2-api.

        ``None`` before the first attempt — stage 2 sends ``NodeHealth.blah2``
        as an explicit ``null`` rather than guessing, which is what the spec's
        "known to be unknown" means.
        """
        return self._last_poll_ok

    @property
    def last_adsb_present(self) -> bool | None:
        """Whether the last parsed frame carried ADS-B associations.

        ``None`` until a frame has been parsed. Sticky afterwards: it reflects
        the last frame we actually saw rather than resetting when a poll fails,
        because a node whose blah2 is down has not stopped having ADS-B
        configured, and ``NodeHealth.blah2`` already says not to trust the rest.

        Note the spec cannot express "I have not looked yet" for this field —
        ``adsb`` is optional and its absence means *disabled*, so the window
        before the first frame is reported as disabled. The spec calls a
        dedicated ``disabled`` value an open item; until then this is the
        closest honest thing.
        """
        return self._last_adsb_present

    @property
    def last_error(self) -> str | None:
        return self._last_error
