"""``Consent`` + identity + config → ``RegisterRequest``.

Sent once per node lifetime, plus operator reactivation. It is the payload with
the most sources feeding it and the only one currently blocked on work outside
this repo — both of the blocking open questions land here.
"""

from __future__ import annotations

import re

from retina_telemetry.collect.consent import Consent
from retina_telemetry.collect.identity import NODE_ID_PATTERN
from retina_telemetry.collect.node_config import NodeConfigRaw
from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.models import (
    AcceptanceRecord,
    Agreements,
    PublicationChoice,
    RegisterRequest,
)


class IncompletePayload(Exception):
    """Registration cannot be built from what this node knows.

    Distinct from a server refusal. A ``403`` means "not now, retry"; this means
    "there is nothing to retry with until something changes locally", so the
    caller should surface it rather than back off.
    """


class UnsupportedNodeId(IncompletePayload):
    """This node's identity is valid, but the ingest spec will not carry it.

    A subclass so that anything already handling :class:`IncompletePayload`
    keeps working, and separate because what has to change is different. An
    incomplete payload waits on the owner or on retina-gui; this waits on the
    **server's spec**, and nothing done on the node will resolve it.
    """


def spec_node_id_pattern() -> str | None:
    """The ``node_id`` pattern the server's ingest spec currently enforces.

    Read out of the generated model rather than written down a second time.
    ``wire/models.py`` is derived from ``docs/node-ingest-v1.yml``, which is the
    contract, so regenerating it against a spec that accepts the current
    node_id format **lifts the registration gate by itself**. A copy of the
    pattern here would be a second place to remember, and therefore the place
    that gets forgotten.

    Returns:
        The pattern, or ``None`` if the spec stops constraining the field, in
        which case there is nothing to gate on.
    """
    for constraint in RegisterRequest.model_fields["node_id"].metadata:
        pattern = getattr(constraint, "pattern", None)
        if pattern:
            return str(pattern)
    return None


def spec_accepts_node_id(node_id: str) -> bool:
    """Whether the server would accept this ``node_id`` as things stand.

    Wider than :data:`~retina_telemetry.collect.identity.NODE_ID_PATTERN` on
    purpose, and currently narrower in practice. A node reads and reports an id
    in either format, but only one of them is in the spec, so a migrated node
    is **deliberately held back from registering** rather than being allowed to
    build a payload that the generated model would reject with a validation
    error nobody could act on.

    Held back, not broken: the node keeps running, keeps its identity, and says
    plainly why it is waiting. It registers on its own once the spec moves.
    """
    pattern = spec_node_id_pattern()
    return True if pattern is None else re.match(pattern, node_id) is not None


def build_registration(
    *,
    node_id: str,
    board_model: str | None,
    consent: Consent,
    config: NodeConfigRaw,
) -> RegisterRequest:
    """Assemble the registration payload.

    | Wire field | Source |
    |---|---|
    | ``node_id`` | ``collect.identity.read_node_id()`` |
    | ``board_model`` | ``collect.identity.read_board_model()`` |
    | ``agreements.licence`` | ``consent.licence`` |
    | ``agreements.remote_management`` | ``consent.remote_management`` |
    | ``agreements.publication`` | ``consent.publication`` |
    | ``config`` | ``collect.node_config.read_config()``, via ``build_node_config`` |

    Args:
        node_id: from ``/data/mender/node_id``, never derived. The generated
            model re-checks the spec's ``^ret[0-9a-f]{8}$`` pattern, so
            ``"Unknown"`` — which retina-gui's own reader returns on failure —
            cannot reach the wire even if it somehow reached this call.
        board_model: the Mender device type, e.g. ``pi5-v3-arm64``. Required by
            the spec but diagnostic only, so an unreadable one is reported as
            ``"unknown"`` rather than blocking registration — losing a
            diagnostic must not strand a node.
        consent: from ``collect.consent.read_consent()``. All three records are
            required by ``Agreements``, and none of them is ever synthesised:
            a missing record means the owner was not shown that text.
        config: from ``collect.node_config.read_config()``.

    Raises:
        UnsupportedNodeId: if the node's id is in a format the ingest spec does
            not yet carry. Waits on the spec, not on anything here.
        IncompletePayload: if any consent record is absent. That means local work
            is outstanding, not that the server said no.
    """
    # Checked before consent, because no amount of local work clears it. An
    # owner who completes the wizard on a migrated node would otherwise be told
    # their agreements were missing, fix that, and get no further.
    #
    # Only for an id that is genuinely one of ours. Something that is not a
    # node_id at all - "Unknown", the config placeholder, a truncated value -
    # is not a node waiting on the spec, and saying so would send whoever reads
    # it looking for a migration that never happened. Those keep falling
    # through to the generated model, whose validation error is the honest
    # answer: this is not a node_id.
    if NODE_ID_PATTERN.match(node_id) and not spec_accepts_node_id(node_id):
        raise UnsupportedNodeId(
            f"node_id {node_id!r} is valid but the ingest spec still requires "
            f"{spec_node_id_pattern()!r}. This node has been migrated to the current "
            "format and cannot register until the server's spec accepts it and "
            "wire/models.py is regenerated. Nothing on the node will resolve this."
        )

    if not consent.complete:
        raise IncompletePayload(
            f"missing consent records: {', '.join(consent.missing)}. Registration requires all "
            "three, and none of them can be manufactured here — a missing record means the owner "
            "was never shown that text. retina-gui's setup wizard is meant to write them "
            "and does not yet."
        )

    wire_config = build_node_config(config)

    # Narrowed by `consent.complete`, which mypy cannot see.
    assert consent.licence and consent.remote_management and consent.publication

    return RegisterRequest(
        node_id=node_id,
        # Diagnostic only, so never worth failing over. The spec requires the
        # field but constrains only its length, and v1.1.1's own description
        # endorses the Mender device type — so "pi5-v3-arm64", not their old
        # "raspberrypi5-4gb" example.
        board_model=board_model or "unknown",
        agreements=Agreements(
            # Stage 1 passes the timestamps through as stored; the generated
            # models parse them as aware datetimes, so a naive or malformed one
            # fails here rather than being silently sent.
            licence=AcceptanceRecord(
                version=consent.licence.version,
                accepted_at=consent.licence.accepted_at,
            ),
            remote_management=AcceptanceRecord(
                version=consent.remote_management.version,
                accepted_at=consent.remote_management.accepted_at,
            ),
            publication=PublicationChoice(
                version=consent.publication.version,
                accepted_at=consent.publication.accepted_at,
                choice=consent.publication.choice,
            ),
        ),
        config=wire_config,
    )
