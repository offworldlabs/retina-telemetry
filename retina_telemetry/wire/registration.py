"""``Consent`` + identity + config → ``RegisterRequest``.

Sent once per node lifetime, plus operator reactivation. It is the payload with
the most sources feeding it and the only one currently blocked on work outside
this repo — both of the blocking open questions land here.
"""

from __future__ import annotations

from retina_telemetry.collect.consent import Consent
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
        IncompletePayload: if any consent record is absent. That means local work
            is outstanding, not that the server said no.
    """
    if not consent.complete:
        raise IncompletePayload(
            f"missing consent records: {', '.join(consent.missing)}. Registration requires all "
            "three, and none of them can be manufactured here — a missing record means the owner "
            "was never shown that text. They are written by retina-gui's setup wizard "
            "(open question Q2)."
        )

    wire_config = build_node_config(config)

    # Narrowed by `consent.complete`, which mypy cannot see.
    assert consent.licence and consent.remote_management and consent.publication

    return RegisterRequest(
        node_id=node_id,
        # Diagnostic only, so never worth failing over. The spec requires the
        # field but says nothing about its vocabulary — see Q15, which tells the
        # server author to expect "pi5-v3-arm64" rather than their example.
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
