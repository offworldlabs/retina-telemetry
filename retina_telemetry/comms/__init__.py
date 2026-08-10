"""Stage 3 — communication.

Knows about HTTP, retries and the server's vocabulary. Knows nothing about
radar: a bistatic delay appearing anywhere in this package means the boundary
has leaked. Payloads arrive already built by ``wire/`` and are sent as-is.

    3a  client.py, levels.py     the machinery, shared by all four endpoints
    3b  lifecycle.py             the state machine and registration gating
    3c  reliable.py, stream.py   the two traffic disciplines

The split between ``client`` and ``levels`` is the one worth understanding.
``client`` performs one request and reports *what happened* without deciding
what to do about it; ``levels`` interprets that outcome against the spec and
updates shared state. Retry policy belongs to neither — it differs between the
two disciplines in 3c, which is the whole reason the machinery is separate.
"""

from retina_telemetry.comms.client import Backoff, Client, Kind, Outcome
from retina_telemetry.comms.levels import apply_response

__all__ = ["Backoff", "Client", "Kind", "Outcome", "apply_response"]
