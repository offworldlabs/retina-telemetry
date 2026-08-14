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

#: Total bytes ``errors`` may occupy once rendered.
#:
#: The spec caps a heartbeat body at 8 KiB **at the origin, ahead of parsing**,
#: and the other two bounds compose straight past it: 20 items of 512
#: characters is 10 KiB of errors alone. A node carrying twenty distinct long
#: faults is exactly the node whose heartbeat matters, and it would have been
#: rejected before anything read it.
#:
#: 6 KiB leaves generous room for the rest of a beat, which is a few hundred
#: bytes. Faults are dropped least-frequent-first to fit, same as the count
#: bound, so the persistent problem survives and the noise goes.
MAX_ERRORS_BYTES = 6 * 1024

#: The spec bounds ``errors`` items at 512 characters, and says anything beyond
#: the list bound is dropped node-side rather than truncating the request. A
#: message long enough to hit this is a traceback that lost its way, and the
#: first 512 characters of one are the useful part anyway.
#:
#: Enforced in :func:`_render`, on the finished string. Enforcing it only on the
#: way in is not enough: the " (xN)" repeat suffix is appended afterwards and
#: pushed the result over the bound.
MAX_MESSAGE = 512


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
        if len(message) > MAX_MESSAGE:
            message = message[: MAX_MESSAGE - 1] + "…"
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
    """Render each fault, truncating the *result* rather than the input.

    The bound has to be applied here and not only in :meth:`Errors.add`,
    because the repeat suffix is added after storage. Truncating on the way in
    to exactly ``MAX_MESSAGE`` and then appending " (x2)" produced 517
    characters against the spec's ``maxLength: 512``, which failed
    ``HeartbeatRequest`` validation — and since the payload is built inside an
    unguarded factory, it stopped the heartbeat for the lifetime of the process.
    A node going permanently silent over a long log line is the exact outcome
    the rest of the heartbeat design exists to avoid.
    """
    rendered: list[str] = []
    budget = MAX_ERRORS_BYTES
    for message, count in counts.most_common():
        one = _render_one(message, count)
        # +3 for the JSON quotes and separator this entry costs in the array.
        cost = len(one.encode()) + 3
        if cost > budget:
            # most_common() is descending, so everything left is rarer than
            # what is already in. Stopping keeps the persistent fault and
            # drops the noise, which is the same trade the count bound makes.
            break
        budget -= cost
        rendered.append(one)
    return rendered


def _render_one(message: str, count: int) -> str:
    """One fault, within the bound, keeping the repeat count.

    The count is the half worth protecting: "seen 300 times" is what separates a
    wedged node from a blip, and it is a handful of characters against a
    truncated traceback nobody reads to the end. So the suffix is reserved and
    the *message* gives way, rather than clamping the finished string and losing
    the count off the end.
    """
    if count == 1:
        return message if len(message) <= MAX_MESSAGE else message[: MAX_MESSAGE - 1] + "…"

    suffix = f" (x{count})"
    room = MAX_MESSAGE - len(suffix)
    if len(message) <= room:
        return message + suffix
    return message[: room - 1] + "…" + suffix
