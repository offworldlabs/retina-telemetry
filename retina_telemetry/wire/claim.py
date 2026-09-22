"""``Nomination`` → ``NodeClaimRequest``.

As thin as the contact builder and for the same reason: retina-gui stores the
wire's own field name, so there is nothing to convert. What this contributes is
the boundary and the one bound that matters.
"""

from __future__ import annotations

from retina_telemetry.collect.claim import Nomination
from retina_telemetry.wire.models import NodeClaimRequest


def build_claim(nomination: Nomination) -> NodeClaimRequest:
    """Convert the owner's nomination into the wire payload.

    | Wire field | Source | Conversion |
    |---|---|---|
    | ``email`` | ``nomination.email`` | none, trimmed and lower cased by the server |

    Sent as the owner typed it. The server states that it strips surrounding
    whitespace and lower cases before judging, so normalising here would only
    be a second implementation of a rule that already exists on the other side,
    and one that could disagree with it.

    ``NodeClaimRequest`` caps the address at 255 and forbids extra properties,
    so a hand-edited file fails here rather than being refused as
    ``invalid_claim`` a second later with nothing to show the owner.

    Raises:
        ValueError: via pydantic, if there is no address or it exceeds the cap.
            The caller checks for an absent address first, because a node with
            nobody claiming it is the ordinary state rather than an error.
    """
    return NodeClaimRequest(email=nomination.email)
