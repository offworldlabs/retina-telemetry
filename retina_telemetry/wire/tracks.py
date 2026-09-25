"""``TrackerFrame`` → ``DetectionFrame.tracker`` and ``tracks``.

Contract 1.6.0 lets a frame carry the node tracker's confirmed tracks. The
frame and its tracks are one unit: each ``active`` track names the detection it
took by index into this frame's arrays, and a frame whose tracks do not add up
is refused whole. So the tracks are built after the arrays, against the indices
that actually went out.

## Why this keeps state

Every other builder here is a pure function of what stage 1 read. This one
cannot be, because of one rule: a track that ends is sent **once more**, as
``deleted``. The tracker reports a death on the frame it happened in, and
latest-wins transport means that frame may never be polled at all. Taken
frame by frame, a track that died between two polls would simply stop
appearing, and the server would be left to guess.

So :class:`TrackLedger` remembers which tracks this process has told the server
are alive, and the last thing it said about each. A remembered track missing
from the tracker's live set is sent as ``deleted`` on the next frame that goes
out, carrying the tracker's final values when the tracker reported the death on
this frame and the last ones sent otherwise.

The ledger is updated when a frame is built, not when it is delivered. A frame
lost in transport loses whatever it carried, a deletion included, which is the
discipline every other field lives under. The server has to age tracks out
anyway, since a node can lose power mid-track.

## A new run is a new namespace

The tracker's ids restart after the process does, and ``tracker.run`` is what
tells the two apart. A change of run forgets every remembered track rather than
declaring it deleted, because a deletion would name the old run's ids under the
new run, where they mean a different track or nothing.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from retina_telemetry.collect.tracker import TrackerFrame, TrackRaw
from retina_telemetry.wire.models import Track, TrackerRun, TrackState
from retina_telemetry.wire.units import ms_to_s

log = logging.getLogger(__name__)

#: The spec's ``maxItems`` on ``DetectionFrame.tracks``.
MAX_TRACKS = 32

_ALIVE = (TrackState.active, TrackState.coasting)


class TrackLedger:
    """What this process has told the server about the tracker's tracks."""

    def __init__(self) -> None:
        self._run: str | None = None
        #: id → the last ``Track`` sent for it as alive.
        self._alive: dict[str, Track] = {}

    def settle(
        self,
        frame: TrackerFrame | None,
        sent_index: dict[int, int],
    ) -> tuple[TrackerRun | None, list[Track] | None]:
        """The frame's ``tracker`` and ``tracks``.

        | Wire field | Source | Conversion |
        |---|---|---|
        | ``tracker.run`` | ``TrackerFrame.run`` | none |
        | ``tracks[].id`` / ``n_associated`` / ``n_missed`` / ``adsb_hex`` / ``is_anomalous`` / ``anomaly_types`` / ``max_velocity_ms`` / ``shadow_fraction`` / ``interference_fraction`` | ``TrackRaw`` | none |
        | ``tracks[].state`` | ``TrackRaw.state`` | none, or ``deleted`` for a remembered track the tracker no longer holds |
        | ``tracks[].hit`` | ``TrackRaw.hit`` | index as blah2-api sent it → index as this frame sends it |
        | ``tracks[].born_t`` | ``TrackRaw.born_timestamp_ms`` | ÷ 1000 → float seconds |
        | ``tracks[].avg_snr`` | ``TrackRaw.avg_snr_db`` | none, already dB |

        Args:
            frame: from ``collect.tracker.TrackerClient.frame``. ``None`` means
                no tracker could be reached, and the frame then carries neither
                field, which is how a node says it sends no tracks.
            sent_index: maps each detection's index in blah2-api's arrays to
                its index in the arrays this frame sends. A detection missing
                from it was not sent (dropped as non-finite, or past the
                spec's 512), so no track can name it.

        Returns:
            ``(None, None)`` with no tracker; ``(run, None)`` when the tracker
            holds nothing for this frame, which says nothing about its tracks;
            otherwise ``(run, tracks)``, where ``[]`` is a tracker holding no
            confirmed track.
        """
        if frame is None:
            return None, None
        if frame.run != self._run:
            self._run = frame.run
            self._alive = {}
        try:
            run = TrackerRun(run=frame.run)
        except ValidationError:
            log.warning("tracker run %r is not one the spec accepts; sending no tracks", frame.run)
            return None, None
        if frame.tracks is None:
            return run, None

        active: list[Track] = []
        coasting: list[Track] = []
        ended: dict[str, TrackRaw] = {}
        live_ids: set[str] = set()
        for raw in frame.tracks:
            if raw.state == TrackState.deleted:
                ended[raw.id] = raw
                continue
            live_ids.add(raw.id)
            if (track := self._alive_track(raw, sent_index)) is not None:
                (active if track.state == TrackState.active else coasting).append(track)

        deleted = [
            self._deleted_track(ended.get(track_id), last)
            for track_id, last in self._alive.items()
            if track_id not in live_ids
        ]
        deleted = [track for track in deleted if track is not None]

        # Active first, because only they place anything; then coasting. A
        # deletion that does not fit stays remembered and goes on the next
        # frame, so the cap costs it nothing but a frame's delay.
        sent = (active + coasting + deleted)[:MAX_TRACKS]
        if len(active) + len(coasting) + len(deleted) > MAX_TRACKS:
            log.warning(
                "sending %d of %d tracks, the spec's limit",
                MAX_TRACKS,
                len(active) + len(coasting) + len(deleted),
            )

        for track in sent:
            if track.state in _ALIVE:
                self._alive[track.id] = track
            else:
                self._alive.pop(track.id, None)
        return run, sent

    def _alive_track(self, raw: TrackRaw, sent_index: dict[int, int]) -> Track | None:
        hit = None
        if raw.state == TrackState.active:
            if raw.hit is None or raw.hit not in sent_index:
                # The detection it took did not go out, and an active track
                # must name one this frame carries. Left out of this frame
                # rather than demoted: it is not coasting, and saying so would
                # be false. It stays remembered if it was, so it is not
                # declared dead either.
                log.debug("track %s took a detection this frame does not send", raw.id)
                return None
            hit = sent_index[raw.hit]
        return _build(raw, state=raw.state, hit=hit)

    @staticmethod
    def _deleted_track(final: TrackRaw | None, last: Track) -> Track | None:
        if final is not None:
            return _build(final, state=TrackState.deleted, hit=None)
        return last.model_copy(update={"state": TrackState.deleted, "hit": None})


def _build(raw: TrackRaw, *, state: str, hit: int | None) -> Track | None:
    """One track, or ``None`` if the spec refuses it.

    A refused track costs that track rather than the frame, the same trade the
    ``adsb`` tags make. A frame's tracks are validated as a set on the server,
    but only for the rules :meth:`TrackLedger.settle` already keeps: ids unique,
    hits unique and inside the arrays, a hit exactly when active.
    """
    try:
        return Track(
            id=raw.id,
            state=state,
            hit=hit,
            n_associated=raw.n_associated,
            n_missed=raw.n_missed,
            adsb_hex=raw.adsb_hex,
            is_anomalous=raw.is_anomalous,
            anomaly_types=raw.anomaly_types,
            max_velocity_ms=raw.max_velocity_ms,
            born_t=ms_to_s(raw.born_timestamp_ms) if raw.born_timestamp_ms is not None else None,
            avg_snr=raw.avg_snr_db,
            shadow_fraction=raw.shadow_fraction,
            interference_fraction=raw.interference_fraction,
        )
    except ValidationError as exc:
        detail = exc.errors()[0] if exc.errors() else exc
        log.warning("discarding track %s: %s", raw.id, detail)
        return None
