"""``Consent`` + identity + config → ``RegisterRequest``.

Sent once per node lifetime, plus reflash recovery. It is the payload with the
most sources feeding it and the only one currently blocked on work outside this
repo — both of the blocking open questions land here.
"""

from __future__ import annotations

from retina_telemetry.collect.consent import Consent
from retina_telemetry.collect.node_config import NodeConfigRaw
from retina_telemetry.wire.config import IncompleteConfig, build_node_config
from retina_telemetry.wire.models import Agreement, RegisterRequest


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
    | ``agreement`` | ``consent.agreement`` |
    | ``config`` | ``collect.node_config.read_config()``, via ``build_node_config`` |

    Args:
        node_id: from ``/data/mender/node_id``, never derived. The generated
            model re-checks the spec's ``^ret[0-9a-f]{8}$`` pattern, so
            ``"Unknown"`` — which retina-gui's own reader returns on failure —
            cannot reach the wire even if it somehow reached this call.
        board_model: the Mender device type, e.g. ``pi5-v3-arm64``. Required by
            the spec but diagnostic only, so an unreadable one is reported as
            the empty-ish string ``"unknown"`` rather than blocking
            registration — losing a diagnostic must not strand a node.
        consent: from ``collect.consent.read_consent()``. Both halves matter:
            an owner who has not opted in must not be registered, and the spec
            requires the acceptance record itself.
        config: from ``collect.node_config.read_config()``.

    Raises:
        IncompletePayload: if consent is absent, or the configuration cannot be
            built. Both mean local work is outstanding, not that the server
            said no.
    """
    if not consent.opted_in:
        raise IncompletePayload(
            "telemetry is not opted in on this node — nothing may be sent to the server. "
            "The record is written by retina-gui's setup wizard (open question Q2)."
        )

    if consent.agreement is None:
        raise IncompletePayload(
            "no agreement acceptance record: RegisterRequest.agreement requires "
            "{version, accepted_at} and the consent record carries neither (open question Q2)."
        )

    try:
        wire_config = build_node_config(config)
    except IncompleteConfig as exc:
        raise IncompletePayload(str(exc)) from exc

    return RegisterRequest(
        node_id=node_id,
        # Diagnostic only, so never worth failing over. The spec requires the
        # field but says nothing about its vocabulary — see Q15, which tells the
        # server author to expect "pi5-v3-arm64" rather than their example.
        board_model=board_model or "unknown",
        agreement=Agreement(
            version=consent.agreement.version,
            # Stage 1 passes this through as stored; the generated model parses
            # it as an aware datetime, so a naive or malformed timestamp fails
            # here rather than being silently sent.
            accepted_at=consent.agreement.accepted_at,
        ),
        config=wire_config,
    )
