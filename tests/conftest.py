"""Fixtures shared across the suite.

The consent records live here because four modules need them and the spec now
requires three separately versioned entries — repeating that shape per file is
how it drifts.
"""

from retina_telemetry.collect.consent import AcceptanceRecord, Consent, PublicationChoice

ACCEPTED_AT = "2026-07-31T09:12:00Z"
VERSION = "2026-07-01"

#: What retina-gui's wizard will write, and what the wire's ``Agreements``
#: object mirrors one-for-one.
CONSENT_FILE = {
    "licence": {"version": VERSION, "accepted_at": ACCEPTED_AT},
    "remote_management": {"version": VERSION, "accepted_at": ACCEPTED_AT},
    "publication": {"version": VERSION, "accepted_at": ACCEPTED_AT, "choice": "public"},
}


def consented(**overrides) -> Consent:
    """A fully accepted node, with any record replaceable."""
    return Consent(
        **{
            "licence": AcceptanceRecord(VERSION, ACCEPTED_AT),
            "remote_management": AcceptanceRecord(VERSION, ACCEPTED_AT),
            "publication": PublicationChoice(VERSION, ACCEPTED_AT, "public"),
            **overrides,
        }
    )
