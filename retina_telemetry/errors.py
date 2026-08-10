"""A bounded record of what has gone wrong since the last heartbeat.

``HeartbeatRequest.errors`` is a list rather than a single slot so that
transient faults between beats are not lost — a blah2 poll that failed twice
and recovered is worth knowing about even though nothing is wrong by the time
the beat goes out.

Bounded because the beat is every 60 s and a wedged node can produce a fault
per poll. An unbounded list would grow to hundreds of near-identical strings
and make the payload useless for the thing it exists to convey. Deduplicated
for the same reason: 120 copies of "detection poll failed: connection refused"
say nothing that one copy and a count do not.

## Cleared on acknowledgement, not on send

The spec is explicit that the list can be cleared "once a beat is
acknowledged", and the difference matters more than it looks. Clearing when the
payload is *built* discards faults whenever the send then fails — which is
exactly when faults are accumulating, because an unreachable server is itself
one of them.

So a beat :meth:`take`\\ s a batch, sends it, and only :meth:`Batch.commit`\\ s
on a 2xx. Anything recorded while that request was in flight survives, because
the batch removes the counts it captured rather than emptying the whole thing.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field

#: Enough to see several distinct faults in one beat without the payload
#: becoming a log file. Beyond this the least frequent distinct fault is
#: dropped.
DEFAULT_LIMIT = 20


@dataclass
class Batch:
    """What one heartbeat is carrying, and the means to clear exactly that."""

    messages: list[str]
    _taken: Counter[str] = field(repr=False)
    _owner: Errors = field(repr=False)
    _committed: bool = field(default=False, repr=False)

    def commit(self) -> None:
        """Discard the faults this batch carried, after the beat was accepted.

        Only these. A fault recorded while the request was in flight was never
        sent and stays for the next beat.
        """
        if self._committed:
            return
        self._committed = True
        self._owner._discard(self._taken)  # noqa: SLF001 - the batch is its collaborator

    def __len__(self) -> int:
        return len(self.messages)


class Errors:
    """Thread-safe, bounded, de-duplicating.

    Every loop can add to it and the heartbeat drains it, so it is shared
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
            return _render(self._counts)

    def take(self) -> Batch:
        """Capture the current faults for a beat that is about to be sent.

        Nothing is removed until :meth:`Batch.commit`, so a beat that never
        lands loses nothing.
        """
        with self._lock:
            taken = Counter(self._counts)
        return Batch(messages=_render(taken), _taken=taken, _owner=self)

    def _discard(self, taken: Counter[str]) -> None:
        with self._lock:
            self._counts.subtract(taken)
            # subtract() can leave zero and negative counts behind.
            self._counts = Counter({k: v for k, v in self._counts.items() if v > 0})

    def __len__(self) -> int:
        with self._lock:
            return len(self._counts)


def _render(counts: Counter[str]) -> list[str]:
    return [
        message if count == 1 else f"{message} (x{count})"
        for message, count in counts.most_common()
    ]
