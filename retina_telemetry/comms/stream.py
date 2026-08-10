"""The latest-wins discipline: detections.

The opposite of ``reliable.py``. A frame that fails is dropped rather than
retried, because by the time a retry could land there is a newer frame worth
more. The server's multi-node association window is 4 s, so a late frame is
associated *wrongly* rather than simply ignored — which makes a stale frame
worse than a missing one, not merely less useful.

Dropped frames are correct behaviour. `seq` gaps are how the server sees them,
and under this discipline they are deliberate and constant rather than a fault.

## The slot is the discipline

:class:`Slot` is a one-element mailbox where writing replaces whatever is
there. That single property *is* "latest wins, at most one in flight, no
queue" — there is no separate rule to enforce and no queue that could grow.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from retina_telemetry.comms.client import Client, Kind
from retina_telemetry.comms.levels import apply_response
from retina_telemetry.state import State

log = logging.getLogger(__name__)

DETECTION_PATH = "/nodes/detection"

#: Short on purpose. The spec wants a request past its timeout abandoned so the
#: next frame goes out fresh, and at roughly one frame per second anything
#: longer means sending a frame the tracker can no longer use.
DETECTION_TIMEOUT_S = (2.0, 3.0)


class Slot:
    """A one-element mailbox. Writing replaces whatever is waiting.

    Not a queue with a maximum size of one: a full queue rejects the newcomer,
    and here the newcomer is the one worth keeping.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: Any = None
        self._ready = threading.Event()
        self._replaced = 0

    def put(self, item: Any) -> None:
        """Publish, replacing anything not yet taken."""
        with self._lock:
            if self._item is not None:
                self._replaced += 1
            self._item = item
            self._ready.set()

    def take(self, timeout: float | None = None) -> Any:
        """Take what is waiting, blocking up to ``timeout``.

        Returns ``None`` if nothing arrived, which is an ordinary outcome —
        blah2 produces roughly one frame a second and the sender asks more
        often than that.
        """
        if not self._ready.wait(timeout):
            return None
        with self._lock:
            item, self._item = self._item, None
            self._ready.clear()
        return item

    @property
    def replaced(self) -> int:
        """Frames superseded before they were sent.

        Not an error count. This is the discipline working — it is only worth
        watching because a number that climbs faster than the frame rate means
        the sender is not keeping up at all.
        """
        with self._lock:
            return self._replaced


class DetectionStream:
    """Sends whatever is in the slot, once, and never retries it."""

    def __init__(
        self,
        client: Client,
        state: State,
        slot: Slot,
        *,
        timeout_s: tuple[float, float] | float = DETECTION_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._state = state
        self._slot = slot
        self._timeout_s = timeout_s
        self._sent = 0
        self._failed = 0

    def send_pending(self, timeout: float | None = 0.5) -> bool:
        """Take one frame and send it. Returns whether it was accepted.

        Skips entirely when the node may not stream — but still drains the
        slot, so a paused node does not resume by flushing a frame from
        whenever it was paused.
        """
        snapshot = self._state.snapshot()
        frame = self._slot.take(timeout)

        if frame is None:
            return False

        if not snapshot.may_stream:
            # Dropped rather than held. Whatever is in the slot when streaming
            # resumes will be current; this one will not be.
            return False

        outcome = self._client.post(
            DETECTION_PATH,
            frame,
            token=snapshot.token,
            timeout_s=self._timeout_s,
        )
        apply_response(outcome, self._state)

        if outcome.ok:
            self._sent += 1
            _check_accepted(outcome.body, frame)
            return True

        self._failed += 1
        if outcome.kind is Kind.CONFLICT:
            # apply_response has already queued the resend. The frame itself is
            # abandoned: re-sending it after the configuration lands would put a
            # stale timestamp on the wire.
            log.info("detection rejected: unknown config_version, config resend queued")
        elif outcome.kind is Kind.RATE_LIMITED:
            # The skipped frames are dropped rather than accumulated. There is
            # nothing to accumulate them in, which is the point of the slot.
            log.warning("detections rate limited; retry after %ss", outcome.retry_after_s)
        elif outcome.kind is not Kind.UNAUTHORIZED:
            # 401 is already logged once by state.reject_token; anything else is
            # a dropped frame, which is ordinary under this discipline.
            log.debug("detection dropped: %s", outcome.describe())
        return False

    @property
    def sent(self) -> int:
        return self._sent

    @property
    def failed(self) -> int:
        return self._failed


def _check_accepted(body: dict[str, Any] | None, frame: dict[str, Any]) -> None:
    """The spec says a mismatch against what was sent is worth logging."""
    accepted = (body or {}).get("accepted")
    offered = len(frame.get("delay") or [])
    if isinstance(accepted, int) and accepted != offered:
        log.warning("server accepted %d of %d detections", accepted, offered)
