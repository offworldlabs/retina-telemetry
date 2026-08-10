"""A bounded record of what has gone wrong since the last heartbeat.

``HeartbeatRequest.errors`` is a list rather than a single slot so that
transient faults between beats are not lost — a blah2 poll that failed twice
and recovered is worth knowing about even though nothing is wrong by the time
the beat goes out.

Bounded because the beat is every 60 s and a wedged node can produce a fault
per poll. An unbounded list would grow to hundreds of near-identical strings
and make the payload useless for the thing it exists to convey.

Deduplicated for the same reason: 120 copies of "detection poll failed:
connection refused" say nothing that one copy and a count do not.
"""

from __future__ import annotations

import threading
from collections import Counter

#: Enough to see several distinct faults in one beat without the payload
#: becoming a log file. Beyond this the oldest distinct fault is dropped.
DEFAULT_LIMIT = 20


class Errors:
    """Thread-safe, bounded, de-duplicating.

    Every loop can add to it, and the heartbeat drains it, so it is shared
    state — but it is self-contained enough not to belong in ``state.py``,
    which is about facts the node acts on rather than things it reports.
    """

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def add(self, message: str) -> None:
        """Record a fault. Repeats are counted rather than appended."""
        if not message:
            return
        with self._lock:
            if message not in self._counts and len(self._counts) >= self._limit:
                # Drop the least frequent distinct fault rather than the
                # oldest: a fault seen once is likelier to be noise than one
                # seen repeatedly.
                least, _ = min(self._counts.items(), key=lambda item: item[1])
                del self._counts[least]
            self._counts[message] += 1

    def snapshot(self) -> list[str]:
        """Read without clearing, for the status document."""
        with self._lock:
            return [_render(message, count) for message, count in self._counts.most_common()]

    def drain(self) -> list[str]:
        """Read and clear, for a heartbeat that is about to be sent.

        Called when the beat is *built* rather than when it is acknowledged.
        A beat that fails to send loses its errors, which is the right trade:
        the alternative is a list that grows without bound whenever the server
        is unreachable, which is exactly when faults accumulate fastest.
        """
        with self._lock:
            drained = [_render(message, count) for message, count in self._counts.most_common()]
            self._counts.clear()
            return drained

    def __len__(self) -> int:
        with self._lock:
            return len(self._counts)


def _render(message: str, count: int) -> str:
    return message if count == 1 else f"{message} (x{count})"
