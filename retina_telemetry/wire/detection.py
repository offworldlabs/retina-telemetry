"""``DetectionPoll`` → ``DetectionFrame``.

The hot path. Stage 1 has already asserted that the arrays are parallel and
equal-length, so this converts units and attaches the two fields the node's own
state supplies.
"""

from __future__ import annotations

from retina_telemetry.collect.blah2 import DetectionPoll
from retina_telemetry.wire.models import DetectionFrame
from retina_telemetry.wire.units import km_to_us, ms_to_s


def build_detection_frame(
    poll: DetectionPoll,
    *,
    seq: int,
    config_version: int,
) -> DetectionFrame:
    """Convert one polled frame into the wire payload.

    | Wire field | Source | Conversion |
    |---|---|---|
    | ``t`` | ``poll.timestamp_ms`` | ÷ 1000 → float seconds |
    | ``seq`` | argument | — |
    | ``config_version`` | argument | — |
    | ``delay`` | ``poll.delay_km`` | × 3.335641 → µs |
    | ``doppler`` | ``poll.doppler_hz`` | none, already Hz |
    | ``snr`` | ``poll.snr_db`` | none, already dB |
    | ``adsb_hex`` | ``poll.adsb`` | ``.hex`` per entry, or ``[None] * n`` |

    Args:
        poll: from ``collect.blah2.Blah2Client.poll_detection``.
        seq: restart-local monotonic counter, from ``state.py``. Not the same as
            capture continuity — ``seq`` gaps are transport loss, which is
            constant and intended under latest-wins, while a gap in ``t`` larger
            than one CPI is capture loss and is invisible any other way (Q3).
        config_version: server-issued, cached in ``state.py``. Never invented —
            the server returns it and the node adopts whatever comes back.
    """
    return DetectionFrame(
        t=ms_to_s(poll.timestamp_ms),
        seq=seq,
        config_version=config_version,
        delay=km_to_us(poll.delay_km),
        doppler=list(poll.doppler_hz),
        snr=list(poll.snr_db),
        adsb_hex=_adsb_hex(poll),
    )


def _adsb_hex(poll: DetectionPoll) -> list[str | None]:
    """Reduce blah2-api's association objects to the ICAO hex the spec wants.

    ``poll.adsb is None`` means blah2-api sent no ``adsb`` key, which means
    association is disabled on this node — so the spec's parallel array is
    synthesised as all-null rather than omitted, because all four arrays must
    be the same length.

    An entry is an object or ``null``; ``.get("hex")`` rather than ``["hex"]``
    because a malformed association should cost one detection's association,
    not the whole frame.
    """
    if poll.adsb is None:
        return [None] * poll.n_detections
    return [entry.get("hex") if entry is not None else None for entry in poll.adsb]
