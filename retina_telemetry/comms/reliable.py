"""The must-land discipline: registration, configuration, heartbeat.

These three retry until they succeed. Losing a registration strands the node,
losing a configuration leaves the server unable to interpret anything that
follows, and losing a heartbeat throws away the one signal that says a node
exists at all.

The opposite of ``stream.py``, which drops freely — and the reason the two are
separate modules rather than one sender with a flag. Everything about them
differs: what a failure costs, whether a payload is still worth sending a
second later, and whether it is ever correct to give up.

## Sleeping is interruptible

Delays are waited on the stop event rather than with ``time.sleep``. A node
told to shut down while waiting out a 30-minute registration backoff must not
take 30 minutes to notice.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from retina_telemetry.comms.client import Backoff, Client, Kind, Outcome
from retina_telemetry.comms.levels import apply_response
from retina_telemetry.state import State

log = logging.getLogger(__name__)


def send_until_delivered(
    client: Client,
    method: str,
    path: str,
    payload_factory: Callable[[], dict[str, Any] | None],
    *,
    state: State,
    stop: threading.Event,
    backoff: Backoff | None = None,
    token: str | None = None,
    max_attempts: int | None = None,
) -> Outcome | None:
    """Send until the server accepts it, or until there is nothing to be gained.

    Args:
        payload_factory: called before **every** attempt rather than once. A
            heartbeat retried two minutes later should carry the health and
            uptime it has now, not the ones it had when the first attempt
            failed. Returning ``None`` abandons the send — the caller has
            decided there is nothing worth sending any more.
        token: taken once by the caller from a snapshot. Registration passes
            ``None``; it is the request that mints one.
        stop: waited on for every delay, so shutdown is prompt.
        max_attempts: mostly for tests. ``None`` means until delivered or until
            a failure that repeating cannot fix.

    Returns:
        The last outcome, or ``None`` if the factory declined or we were asked
        to stop before the first attempt.
    """
    backoff = backoff or Backoff()
    attempts = 0

    while not stop.is_set():
        payload = payload_factory()
        if payload is None:
            return None

        outcome = client.request(method, path, payload, token=token)
        attempts += 1

        # Applied on every outcome, including failures: a 409 or a 401 is
        # exactly the sort of thing that must reach state, and a body carrying
        # no levels applies none.
        apply_response(outcome, state)

        if outcome.ok:
            backoff.reset()
            return outcome

        if not outcome.retryable:
            # 400 means retrying unchanged will not help. 401 means the token
            # was refused and re-sending it is pointless. 409 means the server
            # will not accept this config_version until a resend has happened,
            # which is now queued.
            log.warning("%s %s not retryable: %s", method, path, outcome.describe())
            return outcome

        if max_attempts is not None and attempts >= max_attempts:
            return outcome

        delay = float(outcome.retry_after_s or 0.0) or backoff.next_delay()
        log.info("%s %s failed (%s); retrying in %.1fs", method, path, outcome.kind, delay)
        if stop.wait(delay):
            return outcome

    return None


def is_fatal_for_config(outcome: Outcome) -> bool:
    """Whether a configuration send failed in a way an operator must fix.

    A 400 on `PUT /nodes/config` means the node cannot stream at all until
    somebody edits the configuration, so it has to reach the status document
    rather than being retried into silence. See Q12.
    """
    return outcome.kind is Kind.INVALID
