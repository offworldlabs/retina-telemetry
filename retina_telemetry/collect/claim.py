"""The address that owns this node, and any ask for another link.

.. code-block:: json

    {"email": "owner@example.com", "send_requested_at": "2026-09-22T11:30:00Z"}

Written by retina-gui, read-only to us, at
``/data/retina-gui/telemetry-claim.json``. Both keys are optional and so is the
whole file: a node nobody has claimed runs exactly as a claimed one does, so an
absent file is a complete answer rather than a gap.

## Not the contact email, however alike the two files look

:mod:`retina_telemetry.collect.contact` carries an ``email`` too, and it is a
different question with a worse failure. That one answers "whom do we ring
about this node", is optional throughout and grants nobody anything. This one
answers "who owns it": the server mails it a link, and opening that link binds
the node to the account behind the address. Substituting one for the other
would mail a stranger a link that hands them somebody's node, so the two are
read from separate files and nothing here ever falls back to the other.

## Two keys, because they mean different kinds of thing

``email`` is **state**. A change in it is a local change, which is what
``PUT /nodes/claim`` is for, and that call is the one that mails.

``send_requested_at`` is an **event**: the owner pressed "send again". It has
to exist separately because re-offering an address the node already holds is
accepted, changes nothing and mails nothing, so a node whose link was declined
sits at ``unclaimed`` with the address still on file and no ``PUT`` will ever
move it. ``POST /nodes/claim/resend`` is the only way out, and this timestamp
is how that ask reaches a service that binds no ports and cannot be called.
Nothing in the stack pushes to us, so an event has to be left somewhere to be
found by polling.

Which of the two calls to make is decided in stage 3, not here. This module
reports what the file says and nothing more.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CLAIM_PATH = Path("/data/retina-gui/telemetry-claim.json")


@dataclass(frozen=True)
class Nomination:
    """What the owner asked for, as retina-gui recorded it.

    The spec's own word for offering an address, and deliberately not
    ``Claim``: :class:`retina_telemetry.state.Claim` is where the claim
    actually stands, which is the server's answer rather than the owner's ask,
    and the two disagree for as long as it takes us to notice this file.

    Frozen, so ``==`` is the change check, the same mechanism as ``Contact``
    and ``NodeConfigRaw``.
    """

    email: str | None = None
    #: When the owner last asked for another link, or ``None`` if they never
    #: have. Parsed here so that a hand-edited value fails once, on the read,
    #: rather than at every comparison downstream.
    send_requested_at: datetime | None = None


def read_nomination(path: Path | str = DEFAULT_CLAIM_PATH) -> Nomination:
    """Read the stored claim document.

    Never raises. An unreadable claim document is indistinguishable in
    consequence from an absent one: the node goes unclaimed, which costs it
    nothing operationally, and it is not a reason to stop streaming.
    """
    document = _load(Path(path))
    if not document:
        return Nomination()

    unknown = sorted(set(document) - {"email", "send_requested_at"})
    if unknown:
        # Named rather than shown: this file is the one that reaches a person.
        log.warning("ignoring unknown claim fields: %s", ", ".join(unknown))

    return Nomination(
        email=_text(document.get("email")),
        send_requested_at=_timestamp(document.get("send_requested_at")),
    )


def _load(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The ordinary state of a node nobody has claimed.
        return None
    except OSError as exc:
        log.warning("%s could not be read: %s", path, exc)
        return None

    try:
        document = json.loads(raw)
    except ValueError as exc:
        log.warning("%s is not valid JSON: %s", path, exc)
        return None

    if not isinstance(document, dict):
        log.warning("%s does not contain an object", path)
        return None
    return document


def _text(value: Any) -> str | None:
    """A usable string, or nothing.

    Never coerced. ``str(12345)`` would turn a hand-edited number into an
    address the owner never typed, and this one gets mailed.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _timestamp(value: Any) -> datetime | None:
    """Parse retina-gui's RFC 3339 stamp, tolerating the ``Z`` suffix.

    An unparseable value is dropped rather than treated as "now": acting on it
    would mail somebody because a file was hand-edited badly, and dropping it
    only costs an owner a second press of a button they are already looking at.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("ignoring unparseable send_requested_at: %r", value)
        return None
