"""Whom to contact about this node.

.. code-block:: json

    {"first_name": "Ada", "last_name": "Lovelace",
     "email": "ada@example.com", "phone": "+441234567890", "country": "GB"}

Written by retina-gui, read-only to us, at
``/data/retina-gui/telemetry-contact.json``. The shape mirrors the wire's
``NodeContact`` one-for-one, so there is no translation to get wrong between
what the owner typed and what the server is told.

## Absent is the ordinary state, not a gap

Every field is optional and so is the whole document. The spec is explicit that
a node with nothing to report never calls the contact endpoint at all, so a
missing file is a complete answer rather than a missing one: the owner skipped
the step, or cleared their details, and those mean the same thing. Nothing here
blocks registration, streaming or the heartbeat, and nothing ever should. It is
carried so that a fault we can see and the owner cannot has somewhere to go.

Kept separate from :mod:`retina_telemetry.collect.consent` for the same reason
retina-gui keeps the files apart: those three records gate registration and are
never synthesised, while this is mutable and optional, and a malformed contact
must not be able to strand a node.

## Nothing is ever substituted

Same discipline as the consent records and the beam geometry. A value the owner
did not give is absent, never a placeholder: these reach a person, and inventing
one would put a wrong name or a wrong number against a real node.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONTACT_PATH = Path("/data/retina-gui/telemetry-contact.json")


@dataclass(frozen=True)
class Contact:
    """The owner's contact details, as they gave them.

    Frozen, so ``==`` is the change check: the spec sends this on local change
    only, and comparing the whole document is what decides there was one. Same
    mechanism as ``NodeConfigRaw``, and for the same reason.
    """

    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    #: ISO 3166-1 alpha-2, and it belongs to the **phone number** rather than to
    #: the owner: the server added it to record which country a contact's phone
    #: number is in.
    country: str | None = None

    @property
    def is_empty(self) -> bool:
        """Whether there is anything at all to send.

        An owner who skipped the step and one who cleared every box arrive here
        identically, and mean the same thing.
        """
        return all(getattr(self, f.name) is None for f in fields(self))


def read_contact(path: Path | str = DEFAULT_CONTACT_PATH) -> Contact:
    """Read the stored contact details.

    Never raises. Unlike the configuration, there is no failure here worth
    stopping for: an unreadable contact document is indistinguishable in
    consequence from an absent one, and neither is a reason to stop streaming.
    A malformed file is logged and treated as empty rather than propagated.
    """
    document = _load(Path(path))
    if not document:
        return Contact()

    known = {f.name for f in fields(Contact)}
    unknown = sorted(set(document) - known)
    if unknown:
        # retina-gui writes exactly the wire's fields, so anything else is a
        # hand-edited file or a version skew. Dropped rather than sent: the
        # payload model forbids extra properties and would refuse the lot.
        log.warning("ignoring unknown contact fields: %s", ", ".join(unknown))

    return Contact(**{name: _text(document.get(name)) for name in known})


def _load(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The ordinary state of a node whose owner skipped the step.
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

    A non-string is dropped rather than coerced: ``str(12345)`` would turn a
    hand-edited number into a phone number the owner never typed. Blank and
    whitespace-only are dropped too, so they cannot travel as a detail.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None
