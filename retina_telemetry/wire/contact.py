"""``Contact`` → ``NodeContact``.

The thinnest builder in stage 2, and deliberately so: retina-gui stores the
wire's own field names, so there is nothing to convert, no units to get wrong,
and no value to derive. What this module contributes is the boundary itself,
and the one rule that boundary carries.
"""

from __future__ import annotations

from retina_telemetry.collect.contact import Contact
from retina_telemetry.wire.models import NodeContact


def build_contact(contact: Contact) -> NodeContact:
    """Convert the stored contact details into the wire payload.

    | Wire field | Source | Conversion |
    |---|---|---|
    | ``first_name`` | ``contact.first_name`` | none |
    | ``last_name`` | ``contact.last_name`` | none |
    | ``email`` | ``contact.email`` | none |
    | ``phone`` | ``contact.phone`` | none |
    | ``country`` | ``contact.country`` | none, ISO 3166-1 alpha-2 |

    **Every field is optional and nullable, and nothing is substituted.** These
    reach a person, so a value the owner did not give travels as an absence
    rather than a placeholder.

    The caller decides whether to send at all. An empty document is a valid
    ``NodeContact`` and would clear whatever the server holds, which is right
    when the owner cleared their details and wrong when they simply never gave
    any. Only ``comms`` knows which of those happened, so
    :attr:`Contact.is_empty` is consulted there rather than here.

    Raises:
        ValueError: via pydantic, if a stored value exceeds the spec's caps.
            retina-gui checks the same limits at the box, so reaching this means
            the file was edited by hand.
    """
    return NodeContact(
        first_name=contact.first_name,
        last_name=contact.last_name,
        email=contact.email,
        phone=contact.phone,
        country=contact.country,
    )
