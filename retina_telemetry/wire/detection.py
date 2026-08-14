"""``DetectionPoll`` → ``DetectionFrame``.

The hot path. Stage 1 has already asserted that the arrays are parallel and
equal-length, so this converts units and attaches the two fields the node's own
state supplies.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from retina_telemetry.collect.blah2 import DetectionPoll
from retina_telemetry.wire.models import DetectionFrame
from retina_telemetry.wire.units import km_to_us, ms_to_s

log = logging.getLogger(__name__)

#: The spec bounds each parallel array. Single figures in practice, so this
#: only fires on something pathological — but the whole frame would be
#: rejected otherwise, and a truncated frame beats no frame.
MAX_DETECTIONS = 512

#: The spec now constrains adsb_hex items. blah2-api emits lowercase ICAO
#: hex, but one malformed entry would cost the entire frame, so anything
#: that does not match becomes null — the same as unassociated.
ICAO_HEX = re.compile(r"^[0-9a-f]{6}$")


def build_detection_frame(
    poll: DetectionPoll,
    *,
    seq: int,
    boot_id: str,
    config_version: int,
) -> DetectionFrame:
    """Convert one polled frame into the wire payload.

    | Wire field | Source | Conversion |
    |---|---|---|
    | ``t`` | ``poll.timestamp_ms`` | ÷ 1000 → float seconds |
    | ``seq`` | argument | — |
    | ``boot_id`` | argument | — |
    | ``config_version`` | argument | — |
    | ``delay`` | ``poll.delay_km`` | × 3.335641 → µs |
    | ``doppler`` | ``poll.doppler_hz`` | none, already Hz |
    | ``snr`` | ``poll.snr_db`` | none, already dB |
    | ``adsb_hex`` | ``poll.adsb`` | ``.hex`` per entry, or ``[None] * n`` |

    Args:
        poll: from ``collect.blah2.Blah2Client.poll_detection``.
        seq: restart-local monotonic counter, from ``state.py``. Not the same as
            capture continuity — ``seq`` gaps are transport loss, which is
            constant and intended under latest-wins, while ``t`` spacing beyond
            one CPI is capture loss. The two are independent, and only the first
            is visible to the server (Q3).
        boot_id: from ``state.py``, distinct per process start. Required on
            every frame rather than the heartbeat alone: a restart between two
            beats would otherwise corrupt the server's gap accounting for up to
            a minute.
        config_version: server-issued, cached in ``state.py``. Never invented —
            the server returns it and the node adopts whatever comes back. Stays
            required and non-null here even though the heartbeat's became
            nullable, because a frame cannot be filed without the geometry it
            was measured against.
    """
    if poll.n_detections > MAX_DETECTIONS:
        log.warning(
            "truncating %d detections to the spec's limit of %d",
            poll.n_detections,
            MAX_DETECTIONS,
        )

    limit = MAX_DETECTIONS
    return DetectionFrame(
        t=ms_to_s(poll.timestamp_ms),
        seq=seq,
        boot_id=boot_id,
        config_version=config_version,
        delay=km_to_us(poll.delay_km[:limit]),
        doppler=list(poll.doppler_hz[:limit]),
        snr=list(poll.snr_db[:limit]),
        adsb_hex=_adsb_hex(poll)[:limit],
    )


def _adsb_hex(poll: DetectionPoll) -> list[str | None]:
    """Reduce blah2-api's association objects to the ICAO hex the spec wants.

    ``poll.adsb is None`` means blah2-api sent no ``adsb`` key, which means
    association is disabled on this node — so the spec's parallel array is
    synthesised as all-null rather than omitted, because all four arrays must
    be the same length.

    An entry is an object or ``null``; ``.get("hex")`` rather than ``["hex"]``
    because a malformed association should cost one detection's association,
    not the whole frame. The same reasoning applies to the spec's
    ``^[0-9a-f]{6}$`` — an entry that does not match becomes null rather than
    failing validation and taking every other detection with it.
    """
    if poll.adsb is None:
        return [None] * poll.n_detections
    return [_hex(entry) for entry in poll.adsb]


def _hex(entry: dict[str, Any] | None) -> str | None:
    if entry is None:
        return None
    value = entry.get("hex")
    if isinstance(value, str) and ICAO_HEX.match(value):
        return value
    if value is not None:
        log.warning("discarding an association whose hex is not ICAO 24-bit: %r", value)
    return None
