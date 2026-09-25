"""retina-tracker client: the node's confirmed tracks, frame by frame.

retina-tracker runs beside blah2 on every node. blah2-api forwards it each
frame over TCP, the same bytes it serves us at ``/api/detection``, so the
tracker's arrays and ours are identical and in identical order. We read its
result for a frame from ``GET /frame?timestamp=<ms>`` on its loopback control
port, keyed by the frame's own timestamp.

Contract 1.6.0 is why this exists: a frame may carry the tracker's confirmed
tracks, each naming the detection it took by index into the frame's arrays. The
tracker's other outputs cannot say that. ``events.jsonl`` writes nothing for a
coasting track and nothing at all when one dies, so ``/frame`` was added to
retina-tracker for this reader.

**Pairing is by timestamp, and a miss is ordinary.** blah2-api stores a frame
and forwards it in the same breath, so polling it can land before the tracker
has finished with that frame. A miss is therefore retried briefly, but only
while the tracker says it is behind rather than past us. A tracker nobody feeds
(``network.tracker_forward.enabled: false``) is never waited on, since it will
never have anything.

**No unit conversion here, and no spec vocabulary.** Timestamps stay in epoch
milliseconds under names that say so, ``hit`` indexes the arrays as blah2-api
sent them, and ``wire/`` does the rest.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:30101"

#: How long one frame may wait for the tracker to catch up. The tracker takes
#: milliseconds a frame and blah2 produces about one a second, so this is
#: generous, and it bounds what a slow tracker can cost the detection stream.
CATCH_UP_S = 0.3
CATCH_UP_STEP_S = 0.05


class MalformedTrackerFrame(ValueError):
    """A tracker answer that cannot be trusted onto the wire."""


@dataclass(frozen=True)
class TrackRaw:
    """One confirmed track as the tracker reports it, in its own units.

    ``state`` is the tracker's word for where the track stands after this
    frame, and it decides ``hit``: an ``active`` track took a detection this
    frame and names its index; any other state names none.
    """

    id: str
    state: str
    hit: int | None
    n_associated: int
    n_missed: int
    adsb_hex: str | None
    is_anomalous: bool
    anomaly_types: list[str]
    max_velocity_ms: float
    born_timestamp_ms: float | None
    avg_snr_db: float | None
    shadow_fraction: float | None
    interference_fraction: float | None


@dataclass(frozen=True)
class TrackerFrame:
    """The tracker's answer for one frame.

    ``tracks`` is ``None`` when the tracker is running but holds no result for
    this frame, which is a different thing from ``[]``: that one ran and holds
    no confirmed track.
    """

    run: str
    timestamp_ms: int
    tracks: list[TrackRaw] | None


def parse_tracks(payload: object) -> list[TrackRaw]:
    """Validate and shape the ``tracks`` of one ``/frame`` body.

    Raises:
        MalformedTrackerFrame: if any entry is not a track we can trust. The
            whole list goes, rather than the entry, because an index that
            cannot be read is one we cannot vouch for the rest of.
    """
    if not isinstance(payload, list):
        raise MalformedTrackerFrame(f"tracks must be an array, got {type(payload).__name__}")
    return [_track(entry) for entry in payload]


def _track(entry: object) -> TrackRaw:
    if not isinstance(entry, dict):
        raise MalformedTrackerFrame(f"a track must be an object, got {entry!r}")
    hit = entry.get("hit")
    if hit is not None and not _is_int(hit):
        raise MalformedTrackerFrame(f"hit must be an integer or null, got {hit!r}")
    anomaly_types = entry.get("anomaly_types")
    if not isinstance(anomaly_types, list) or not all(isinstance(a, str) for a in anomaly_types):
        raise MalformedTrackerFrame(f"anomaly_types must be strings, got {anomaly_types!r}")
    try:
        return TrackRaw(
            id=_str(entry, "id"),
            state=_str(entry, "state"),
            hit=hit,
            n_associated=_int(entry, "n_associated"),
            n_missed=_int(entry, "n_missed"),
            adsb_hex=entry.get("adsb_hex") if isinstance(entry.get("adsb_hex"), str) else None,
            is_anomalous=_bool(entry, "is_anomalous"),
            anomaly_types=list(anomaly_types),
            max_velocity_ms=_required_number(entry, "max_velocity_ms"),
            born_timestamp_ms=_number(entry.get("born_timestamp")),
            avg_snr_db=_number(entry.get("avg_snr")),
            shadow_fraction=_number(entry.get("shadow_fraction")),
            interference_fraction=_number(entry.get("interference_fraction")),
        )
    except KeyError as exc:
        raise MalformedTrackerFrame(f"a track is missing {exc.args[0]}") from None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str(entry: dict[str, Any], key: str) -> str:
    value = entry[key]
    if not isinstance(value, str):
        raise MalformedTrackerFrame(f"{key} must be a string, got {value!r}")
    return value


def _bool(entry: dict[str, Any], key: str) -> bool:
    value = entry[key]
    if not isinstance(value, bool):
        raise MalformedTrackerFrame(f"{key} must be a boolean, got {value!r}")
    return value


def _int(entry: dict[str, Any], key: str) -> int:
    value = entry[key]
    if not _is_int(value):
        raise MalformedTrackerFrame(f"{key} must be an integer, got {value!r}")
    return value


def _required_number(entry: dict[str, Any], key: str) -> float:
    """A number the spec requires. Never substituted: a speed the tracker did
    not give us is not zero."""
    value = _number(entry[key])
    if value is None:
        raise MalformedTrackerFrame(f"{key} must be a finite number, got {entry[key]!r}")
    return value


def _number(value: object) -> float | None:
    """A finite number, or ``None``. Non-finite values are not valid JSON on
    the way out, so they stop here, as they do for detections."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


class TrackerClient:
    """Asks retina-tracker for its result on a given frame.

    Remembers only the last run it heard, so that a frame the tracker does not
    hold can still say which tracker produced nothing for it.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: float = 0.25,
        catch_up_s: float = CATCH_UP_S,
        session: Any = None,
        sleep: Any = time.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._catch_up_s = catch_up_s
        self._sleep = sleep
        self._clock = clock

        if session is None:
            import requests

            session = requests.Session()
        self._session = session
        self._last_error: str | None = None
        self._predates_frame = False

    def frame(self, timestamp_ms: int) -> TrackerFrame | None:
        """The tracker's result for the frame at ``timestamp_ms``.

        Returns:
            ``None`` if the tracker could not be reached or answered with
            something unusable: this node is not running a tracker we can
            speak for. Otherwise a :class:`TrackerFrame`, whose ``tracks`` is
            ``None`` when the tracker is up but holds nothing for this frame.
        """
        deadline = self._clock() + self._catch_up_s
        while True:
            answer = self._get(timestamp_ms)
            if answer is None:
                return None
            status, body = answer
            run = body.get("run")
            if status == 404 and "run" not in body:
                # A tracker released before /frame existed: its control server
                # answers any route it does not know with a bare 404. That is
                # every node until a tracker carrying the route reaches it, so
                # it is an expected state of the rollout rather than a fault,
                # and it stays out of errors[], which the server keeps for its
                # operators. Said once, in the log.
                self._last_error = None
                if not self._predates_frame:
                    log.info("the tracker has no /frame route; sending frames without tracks")
                    self._predates_frame = True
                return None
            if not isinstance(run, str):
                self._last_error = f"tracker answered {status} without a run"
                log.warning("%s", self._last_error)
                return None
            self._predates_frame = False

            if status == 200:
                try:
                    tracks = parse_tracks(body.get("tracks"))
                except MalformedTrackerFrame as exc:
                    self._last_error = f"malformed tracker frame: {exc}"
                    log.warning("discarding the tracker's result for a frame: %s", exc)
                    return TrackerFrame(run=run, timestamp_ms=timestamp_ms, tracks=None)
                self._last_error = None
                return TrackerFrame(run=run, timestamp_ms=timestamp_ms, tracks=tracks)

            # 404: not held. Worth waiting for only if the tracker is being fed
            # and has not yet reached this frame. Past it, the frame is gone;
            # never fed, it will never come.
            self._last_error = None
            latest = body.get("latest")
            behind = _is_int(latest) and latest < timestamp_ms
            if not behind or self._clock() >= deadline:
                return TrackerFrame(run=run, timestamp_ms=timestamp_ms, tracks=None)
            self._sleep(CATCH_UP_STEP_S)

    def _get(self, timestamp_ms: int) -> tuple[int, dict[str, Any]] | None:
        try:
            response = self._session.get(
                f"{self._base_url}/frame",
                params={"timestamp": timestamp_ms},
                timeout=self._timeout_s,
            )
            status = response.status_code
            body = json.loads(response.text)
        except Exception as exc:  # noqa: BLE001 - the poll loop must never die
            # The type rather than the message: requests puts an object address
            # in a connection error, so the text differs on every frame and
            # would defeat the de-duplication in errors.py.
            self._last_error = f"tracker unreachable: {type(exc).__name__}"
            log.debug("tracker unreachable: %s", exc)
            return None
        if status not in (200, 404) or not isinstance(body, dict):
            self._last_error = f"tracker answered {status}"
            log.warning("%s", self._last_error)
            return None
        return status, body

    def close(self) -> None:
        closer = getattr(self._session, "close", None)
        if closer is not None:
            closer()

    @property
    def last_error(self) -> str | None:
        return self._last_error
