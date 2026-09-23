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

#: ``AdsbTag.hex`` is bounded by this. blah2-api emits lowercase ICAO hex,
#: but one malformed entry would cost the entire frame, so anything that does
#: not match becomes null, the same as unassociated.
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
    | ``adsb`` | ``poll.adsb`` | one ``AdsbTag`` per entry carrying an ICAO hex and a finite ``lat``/``lon``; ``null`` otherwise; the column omitted entirely when association is off |

    ``adsb`` carries the association and the position it was made at, so the
    server can file where the aircraft actually was for this detection and
    claim it, and calibrate the node's coverage, without a position source of
    its own. That matters most where the server only ever sees a node
    second-hand, through the detection mirror, since a bare hex cannot be
    placed there at all.

    **``adsb_hex`` is not sent.** Contract 1.5.0 deprecated it, because the tag
    already says which aircraft was matched: sending both put every match on
    the wire twice and nothing on the server read the frame's hex column.
    Sending neither column is how a node says it matched nothing.

    The one thing that costs is an association carrying a usable hex but no
    usable position, which travels as ``null`` where it used to travel as a
    bare hex. The server ignores bare hexes, so nothing downstream notices.

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
    adsb = _adsb_tags(poll)[:limit] if poll.adsb is not None else None

    keep = _finite_indices(delay, doppler, snr)
    if len(keep) != len(delay):
        log.warning("dropping %d detection(s) carrying a non-finite value", len(delay) - len(keep))
        delay = [delay[i] for i in keep]
        doppler = [doppler[i] for i in keep]
        snr = [snr[i] for i in keep]
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

    Dropping the index across every array rather than the frame, on the same
    reasoning as a malformed association: one bad value must not cost the other
    detections in the CPI. Dropping it from *all* of them is what keeps them
    parallel, which the spec requires and the server relies on.
    """
    return [i for i in range(len(arrays[0])) if all(math.isfinite(array[i]) for array in arrays)]


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


def _adsb_tags(poll: DetectionPoll) -> list[AdsbTag | None]:
    """One entry per association, in the order blah2-api reported them.

    Called only when association is on, so ``poll.adsb`` is a list rather than
    ``None``; stage 1 has already asserted it is as long as the other arrays.
    """
    return [_tag(entry) for entry in poll.adsb or []]


def _tag(entry: dict[str, Any] | None) -> AdsbTag | None:
    """One association as an ``AdsbTag``, or ``None`` where nothing is usable.

    The hex is read and validated here rather than taken from a parallel
    column, because there is no longer a parallel column to take it from.

    An association missing either half is dropped whole, and the halves fail
    for different reasons worth keeping apart: a hex that is not ICAO 24-bit is
    a malformed association, while an absent or non-finite ``lat``/``lon`` is
    an association blah2-api could not place. Either way a tag needs both, and
    a value the spec refuses (a latitude past 90, say) costs this one tag
    rather than the frame. That is the same trade every other per-entry rule
    here makes.
    """
    if not isinstance(entry, dict):
        return None
    hexn = _hex(entry)
    if hexn is None:
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
