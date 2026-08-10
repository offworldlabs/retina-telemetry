"""What a response means, applied once for every endpoint.

Every response carries derived state, restated in full each time. Anything the
server needs to tell the node is a **level rather than an edge** — repeated
until the node notices — so there is no need to catch a particular response and
no harm in reading the same value twice.

That is why this is one function rather than four handlers. `config_stale`
arrives identically on a detection ack and a heartbeat response, and a node
that ignored it on one of them would be relying on the other to repeat it.

Two rules here are worth more than the rest of the module:

**A 401 is not a reason to re-register.** Revocation is a `401` rather than a
control field precisely because it has to work whether or not the node is
cooperating, and a node that answers it by registering again turns one
deliberate revocation into a registration storm. The token is kept, the stream
stops, the heartbeat continues so the failure stays visible.

**A 409 is not a reason to retry the frame.** It says the server does not
recognise our `config_version`, so the same request would fail identically
until a configuration resend has happened.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from retina_telemetry.comms.client import Kind, Outcome
from retina_telemetry.state import State

log = logging.getLogger(__name__)

#: How far the node clock may drift from the server's before it is worth
#: saying so. Detection timestamps are node-clock and a Pi 5 has no
#: battery-backed RTC, so a freshly flashed board can be badly wrong until NTP
#: settles — but a second or two of ordinary skew is not news.
CLOCK_WARN_S = 5.0


def apply_response(outcome: Outcome, state: State) -> None:
    """Adopt everything a response implies, whatever endpoint produced it.

    Safe to call for any outcome, including failures — a body that carries no
    levels simply applies none.
    """
    if outcome.kind is Kind.UNAUTHORIZED:
        # Never re-register. See the module docstring.
        state.reject_token()
        return

    if outcome.kind is Kind.CONFLICT:
        state.request_config_resend()
        return

    body = outcome.body or {}
    server_time = _server_time(body.get("server_time"))

    state.apply_levels(
        config_version=_int_or_none(body.get("config_version")),
        config_stale=_bool_or_none(body.get("config_stale")),
        streaming_allowed=_bool_or_none(body.get("streaming_allowed")),
        node_ref=_str_or_none(body.get("node_ref")),
        server_time=server_time,
    )

    if server_time is not None:
        _warn_on_clock_offset(state)


def _warn_on_clock_offset(state: State) -> None:
    offset = state.snapshot().clock_offset_s
    if offset is not None and abs(offset) > CLOCK_WARN_S:
        log.warning(
            "node clock is %.1f s %s the server; detection timestamps are node-clock",
            abs(offset),
            "ahead of" if offset > 0 else "behind",
        )


def _server_time(raw: Any) -> datetime | None:
    """Parse RFC 3339, tolerating the ``Z`` suffix Python did not accept before 3.11."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("ignoring unparseable server_time: %r", raw)
        return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool_or_none(value: Any) -> bool | None:
    """``None`` where the field was absent, which is not the same as false."""
    return value if isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
