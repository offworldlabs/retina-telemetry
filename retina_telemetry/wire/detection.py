"""``DetectionPoll`` → ``DetectionFrame``.

The hot path. Stage 1 has already asserted that the arrays are parallel and
equal-length, so this converts units and attaches the two fields the node's own
state supplies.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from pydantic import ValidationError

from retina_telemetry.collect.blah2 import DetectionPoll
from retina_telemetry.wire.models import AdsbTag, DetectionFrame
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
    | ``adsb`` | ``poll.adsb`` | ``AdsbTag`` per entry whose hex passed AND that carries a finite ``lat``/``lon``; ``null`` otherwise; the whole column omitted when association is off |

    ``adsb`` is the same association as ``adsb_hex`` said with the position
    it was made at (contract 1.5.0).  The server files that position for the
    detection itself, so it — and every environment it mirrors the frame to —
    can claim the detection and calibrate the node's coverage from it without
    a position source of its own.  An entry's hex is taken from ``adsb_hex``
    at the same index, never re-read, so the two columns cannot disagree.

    Args:
        poll: from ``collect.blah2.Blah2Client.poll_detection``.
        seq: restart-local monotonic counter, from ``state.py``. Not the same as
            capture continuity — ``seq`` gaps are transport loss, which is
            constant and intended under latest-wins, while ``t`` spacing beyond
            one CPI is capture loss. The two are independent, and only the first
            is visible to the server.
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
    delay = km_to_us(poll.delay_km[:limit])
    doppler = list(poll.doppler_hz[:limit])
    snr = list(poll.snr_db[:limit])
    adsb_hex = _adsb_hex(poll)[:limit]
    adsb = _adsb_tags(poll, adsb_hex) if poll.adsb is not None else None

    keep = _finite_indices(delay, doppler, snr)
    if len(keep) != len(delay):
        log.warning("dropping %d detection(s) carrying a non-finite value", len(delay) - len(keep))
        delay = [delay[i] for i in keep]
        doppler = [doppler[i] for i in keep]
        snr = [snr[i] for i in keep]
        adsb_hex = [adsb_hex[i] for i in keep]
        if adsb is not None:
            adsb = [adsb[i] for i in keep]

    return DetectionFrame(
        t=ms_to_s(poll.timestamp_ms),
        seq=seq,
        boot_id=boot_id,
        config_version=config_version,
        delay=delay,
        doppler=doppler,
        snr=snr,
        adsb_hex=adsb_hex,
        adsb=adsb,
    )


def _finite_indices(*arrays: list[float]) -> list[int]:
    """Indices where every parallel array holds a finite number.

    ``inf`` and ``nan`` serialise as the bare tokens ``Infinity`` and ``NaN``,
    which are **not valid JSON** — a strict parser rejects the whole body, so one
    bad value costs the entire frame and the loss is opaque from both ends.

    Reachable rather than theoretical: ``snr`` is ``10*log10(|x|) - noisePower``
    (``CfarDetector1D.cpp:48``), so a detection with zero magnitude gives
    ``-inf``. pydantic accepts it because the spec bounds these arrays' length
    and not their values.

    Dropping the index across all four arrays rather than the frame, on the same
    reasoning as a malformed ``adsb_hex``: one bad value must not cost the other
    detections in the CPI. Dropping it from *all* of them is what keeps the four
    parallel, which the spec requires and the server relies on.
    """
    return [i for i in range(len(arrays[0])) if all(math.isfinite(array[i]) for array in arrays)]


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


#: The optional numbers an association may carry, wire name for wire name.
_TAG_FIELDS = (
    "alt",
    "gs",
    "track",
    "expected_delay",
    "expected_doppler",
    "delay_residual",
    "doppler_residual",
)


def _adsb_tags(poll: DetectionPoll, hexes: list[str | None]) -> list[AdsbTag | None]:
    """The associations again, each with the position it was made at.

    Parallel to ``hexes`` (already truncated to the spec's limit), and keyed
    on it: an entry gets a tag only where ``adsb_hex`` carries its hex, so the
    server's entry-for-entry agreement rule holds by construction.
    """
    entries = poll.adsb or []
    return [_tag(entries[i] if i < len(entries) else None, hexn) for i, hexn in enumerate(hexes)]


def _tag(entry: dict[str, Any] | None, hexn: str | None) -> AdsbTag | None:
    """One association as an ``AdsbTag``, or ``None`` where it has no usable position.

    An association without a finite ``lat``/``lon`` is still an association:
    ``adsb_hex`` carries it and only the position is withheld.  A position the
    spec refuses (a latitude past 90, say) costs this one tag, not the frame —
    the same trade every other per-entry rule in this module makes.
    """
    if hexn is None or not isinstance(entry, dict):
        return None
    lat = _finite(entry.get("lat"))
    lon = _finite(entry.get("lon"))
    if lat is None or lon is None:
        return None
    try:
        return AdsbTag(
            hex=hexn, lat=lat, lon=lon, **{k: _finite(entry.get(k)) for k in _TAG_FIELDS}
        )
    except ValidationError as exc:
        detail = exc.errors()[0]["msg"] if exc.errors() else exc
        log.warning("discarding the position of association %s: %s", hexn, detail)
        return None


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)
