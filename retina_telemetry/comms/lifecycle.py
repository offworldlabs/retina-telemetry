"""What the node is doing, and how it gets a token.

Two things live here. :func:`derive_state` answers "what is this node doing
right now" from facts rather than from a mutable field, so the answer cannot
drift out of step with reality. :class:`Registrar` owns the discipline around
``POST /nodes/register``, which is the one request that can neither be rushed
nor abandoned.

## Why the state is derived, not stored

A state machine with an assignable ``self.state`` has one failure mode above all
others: a transition somebody forgot to write. Deriving it from the same values
the rest of the service acts on means the reported state and the actual
behaviour cannot disagree — if ``may_stream`` is false, the state says so,
because they read the same snapshot.

Only ``registering`` is passed in, because "a request is in flight right now" is
genuinely not derivable from state that outlives the request.

## Why this module imports nothing from ``collect``

Stage 3 knows about the server and nothing else. Whether the node has an
identity or a consent record are facts about the node, so they arrive here as
booleans that ``__main__`` computes. Keeping the import out is what stops
``comms`` slowly acquiring opinions about node config.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from retina_telemetry.comms.client import (
    REGISTER_TIMEOUT_S,
    Backoff,
    Client,
    Kind,
    Outcome,
)
from retina_telemetry.state import Snapshot, State
from retina_telemetry.wire.models import NodeState as WireState

log = logging.getLogger(__name__)

REGISTER_PATH = "/nodes/register"

#: Registration is limited to 5/hour and 20/day per node_id, and an ordinary
#: node uses a handful of attempts in its lifetime — so the ceiling is high and
#: the first delay is not small. A broken retry loop should hit the wall of its
#: own backoff long before the server's.
REGISTER_BACKOFF_BASE_S = 30.0
REGISTER_BACKOFF_MAX_S = 1800.0


class NodeState(StrEnum):
    """The node's own account of itself.

    The first five never reach the server: each describes a node that cannot
    send a heartbeat, because ``HeartbeatRequest`` needs a token and a
    ``config_version``. They exist for the status document, which is the only
    thing that can report them.
    """

    #: The owner declined telemetry, or has not been through the wizard. Not a
    #: fault — the container runs, idles, and says so.
    OPTED_OUT = "opted_out"
    #: No usable /data/mender/node_id. The node has not enrolled with Mender,
    #: or has fallen back to a mac= identity. Needs an operator.
    NO_IDENTITY = "no_identity"
    #: Opted in, but no {version, accepted_at} to send. Registration requires
    #: one, so this is as far as the node gets.
    NO_AGREEMENT = "no_agreement"
    #: Everything needed, no token yet.
    UNREGISTERED = "unregistered"
    #: A registration request is in flight, or waiting out a Retry-After.
    REGISTERING = "registering"
    #: Holds a token but no config_version — the state every node passes
    #: through on restart, since the version is not persisted. Cleared by the
    #: first PUT /nodes/config.
    AWAITING_CONFIG = "awaiting_config"

    #: From here on the node can heartbeat, so these are what the server sees.
    STREAMING = "streaming"
    #: Registered and permitted, and blah2 has never produced a frame. Distinct
    #: from NO_DETECTIONS because the spec's `starting` means exactly this — the
    #: window before the radar has produced anything, "where a new owner most
    #: often needs support".
    STARTING = "starting"
    #: blah2 produced frames and has stopped. A working node with a broken
    #: radar, which is a fault rather than a beginning.
    NO_DETECTIONS = "no_detections"
    #: Told to stop by streaming_allowed, still beating.
    PAUSED = "paused"
    #: The server refused our token. Streaming stops, beating continues so the
    #: failure stays visible, and we never re-register.
    REVOKED = "revoked"

    @property
    def reaches_the_server(self) -> bool:
        return self in {
            NodeState.STREAMING,
            NodeState.STARTING,
            NodeState.NO_DETECTIONS,
            NodeState.PAUSED,
            NodeState.REVOKED,
        }

    @property
    def wire(self) -> WireState:
        """The spec's word for this, over its closed set of six.

        Our vocabulary is richer because the status document can report things
        the wire cannot — a node with no identity has no way to say so to the
        server, since it cannot build a heartbeat at all. Everything that does
        reach the server has an honest equivalent here; the rest map to
        `starting`, which is true of them and never actually sent.

        The two faults are deliberately different words, because they point at
        different teams. A radar that produced and then stopped is `stalled`,
        which the spec says to raise against the radar; a refused token is
        `error`, raised against this client. Before v1.1.1 both were `error`,
        which meant a healthy node with a dead blah2 paged the wrong people.
        """
        return {
            NodeState.STREAMING: WireState.streaming,
            NodeState.PAUSED: WireState.paused,
            NodeState.STARTING: WireState.starting,
            NodeState.NO_DETECTIONS: WireState.stalled,
            NodeState.REVOKED: WireState.error,
        }.get(self, WireState.starting)


def derive_state(
    snapshot: Snapshot,
    *,
    has_identity: bool,
    licence_accepted: bool,
    all_records_present: bool,
    registering: bool = False,
    detections_flowing: bool | None = None,
    ever_detected: bool = False,
) -> NodeState:
    """Work out what the node is doing, in order of precedence.

    Order matters and is not arbitrary: it runs from the conditions furthest
    outside our control to those nearest. An opted-out node's missing identity
    is not worth reporting, because nothing would be done about it either way.

    Args:
        detections_flowing: whether blah2-api answered the last poll, from
            ``Blah2Client.last_poll_ok``. ``None`` means we have not polled yet,
            which does not downgrade the state — reporting no detections before
            looking would be a guess, and the first poll is a second away.
        ever_detected: whether blah2 has produced a frame at any point, from
            ``Blah2Client.has_produced``. Separates a radar that has not started
            from one that has stopped, which the spec's vocabulary distinguishes
            and ours would otherwise collapse.
    """
    if not licence_accepted:
        return NodeState.OPTED_OUT
    if not has_identity:
        return NodeState.NO_IDENTITY
    if not all_records_present:
        return NodeState.NO_AGREEMENT

    if not snapshot.registered:
        return NodeState.REGISTERING if registering else NodeState.UNREGISTERED

    # Checked before AWAITING_CONFIG: a node whose token was revoked is not
    # waiting for configuration in any useful sense, and "revoked" is the more
    # actionable thing to report.
    if snapshot.token_rejected:
        return NodeState.REVOKED
    if snapshot.config_version is None:
        return NodeState.AWAITING_CONFIG
    if not snapshot.streaming_allowed:
        return NodeState.PAUSED
    if detections_flowing is False:
        return NodeState.NO_DETECTIONS if ever_detected else NodeState.STARTING
    return NodeState.STREAMING


def explain(state: NodeState) -> str | None:
    """A sentence for the status document, where the state alone is thin."""
    return {
        NodeState.OPTED_OUT: (
            "telemetry is not enabled for this node. Nothing is sent to the server. "
            "Enable it in the retina-gui setup wizard."
        ),
        NodeState.NO_IDENTITY: (
            "/data/mender/node_id is missing or unreadable, so this node has no identity. "
            "It has not enrolled with Mender, or has fallen back to a MAC-based one. "
            "Nothing can be sent until that is resolved."
        ),
        NodeState.NO_AGREEMENT: (
            "no record of the terms being accepted, which registration requires. "
            "Complete the agreements step in the retina-gui setup wizard."
        ),
        NodeState.REGISTERING: (
            "registering. A refusal here is deliberately opaque and is the normal "
            "answer while Mender acceptance propagates; a reflashed board waits until "
            "an operator opens a 24-hour reflash window."
        ),
        NodeState.AWAITING_CONFIG: (
            "registered, but waiting to send its configuration before it can report. "
            "This is normal for a few seconds after a restart."
        ),
        NodeState.STARTING: (
            "registered and waiting for the radar to produce its first frame. Normal shortly "
            "after a start; if it persists, blah2 is not running."
        ),
        NodeState.NO_DETECTIONS: (
            "registered and permitted to stream, but blah2-api has stopped answering after "
            "having worked, so there is nothing to send. The heartbeat continues and reports it."
        ),
        NodeState.REVOKED: (
            "the server rejected this node's token. Detections have stopped; heartbeats "
            "continue so the failure stays visible. The node will not re-register on its "
            "own — that is deliberate."
        ),
    }.get(state)


class Registrar:
    """Owns ``POST /nodes/register`` and the discipline around its refusals.

    Registration failures are deliberately opaque: unknown device, not yet
    accepted by Mender, already holding a valid token, and an identity in
    cooldown all return the same 403 at the same latency. So there is nothing to
    branch on — only a delay to honour and a backoff to grow.
    """

    def __init__(self, client: Client, state: State, *, backoff: Backoff | None = None) -> None:
        self._client = client
        self._state = state
        self._backoff = backoff or Backoff(
            base_s=REGISTER_BACKOFF_BASE_S, maximum_s=REGISTER_BACKOFF_MAX_S
        )

    def attempt(self, payload: dict[str, Any]) -> Outcome:
        """Try once. Stores the token on success; never retries here.

        Retrying is the caller's loop, so a stop signal can interrupt it — a
        method that slept internally could not be shut down promptly.
        """
        outcome = self._client.post(REGISTER_PATH, payload, timeout_s=REGISTER_TIMEOUT_S)

        if outcome.ok:
            body = outcome.body or {}
            token = body.get("token")
            node_ref = body.get("node_ref")
            config_version = body.get("config_version")

            if not isinstance(token, str) or not token:
                # A 200 without a usable token is the server breaking its own
                # contract. Treat it as a failure rather than storing nothing
                # and reporting success.
                return _malformed(outcome, "registration succeeded but carried no token")
            if not isinstance(config_version, int) or isinstance(config_version, bool):
                return _malformed(outcome, "registration succeeded but carried no config_version")

            self._state.store_token(
                token,
                node_ref=node_ref if isinstance(node_ref, str) else "",
                config_version=config_version,
            )
            self._backoff.reset()
            log.info("registered; config_version=%s", config_version)
            return outcome

        if outcome.kind is Kind.INVALID:
            # 400 means the configuration failed validation. Retrying unchanged
            # will not help, so the loop should stop and the operator should
            # see it.
            log.error("registration rejected: %s", outcome.describe())

        return outcome

    def delay_before_retry(self, outcome: Outcome) -> float:
        """How long to wait before trying again.

        ``Retry-After`` wins when the server sent one — it is required on 403
        and 429 and is the server telling us what it wants. Our own backoff
        still advances underneath, so a server that stops sending the header
        does not reset us to a fast loop.
        """
        ours = self._backoff.next_delay()
        if outcome.retry_after_s is not None:
            return max(float(outcome.retry_after_s), 0.0)
        return ours

    @property
    def attempts(self) -> int:
        return self._backoff.attempts


def _malformed(outcome: Outcome, message: str) -> Outcome:
    log.error("%s", message)
    return Outcome(
        kind=Kind.SERVER_ERROR,
        status=outcome.status,
        body=outcome.body,
        retry_after_s=outcome.retry_after_s,
        error=message,
    )
