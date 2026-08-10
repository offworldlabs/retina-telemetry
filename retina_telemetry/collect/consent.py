"""What the owner agreed to, and when.

Three separately versioned records, because the spec says they are withdrawn
separately: withdrawing the publication choice must not terminate the licence or
stop the node.

.. code-block:: json

    {
      "licence":           {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
      "remote_management": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
      "publication":       {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z",
                            "choice": "public"}
    }

Written by retina-gui's setup wizard, read-only to us, at
``/data/retina-gui/telemetry-consent.json``. The shape deliberately mirrors the
wire's ``Agreements`` object one-for-one, so there is no translation to get
wrong between what the owner was shown and what the server is told.

## Nothing here is ever synthesised

A missing record means the owner was not shown that text, so we do not have a
record of them accepting it. The spec notes that a node arriving without a
recorded publication choice is *treated as* ``public`` server-side, and that is
the server's call to make about its own defaults — it is not licence for us to
manufacture an acceptance record with a version and a timestamp nobody
generated. A node missing any of the three cannot register, and says so.

That matters most for ``publication``: the choice governs whether a dwelling's
position ends up in a public archive, and the disclosure version records which
wording the owner actually saw.

Nothing writes this file yet — the wizard's agreements step persists nothing and
the EULA is a placeholder (open question Q2). A missing file is therefore the
state of every node in the fleet today, and is treated as "not accepted" rather
than as an error.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONSENT_PATH = Path("/data/retina-gui/telemetry-consent.json")

#: The three the spec requires, and the only ones it accepts.
LICENCE = "licence"
REMOTE_MANAGEMENT = "remote_management"
PUBLICATION = "publication"


@dataclass(frozen=True)
class AcceptanceRecord:
    """One versioned thing the owner accepted.

    Both fields are passed through as stored. ``accepted_at`` is expected to be
    RFC 3339, but validating that is stage 2's job — this layer reports what the
    file says.
    """

    version: str
    accepted_at: str


@dataclass(frozen=True)
class PublicationChoice:
    """The publication decision, and which disclosure text produced it."""

    version: str
    accepted_at: str
    choice: str

    @property
    def is_public(self) -> bool:
        return self.choice == "public"


@dataclass(frozen=True)
class Consent:
    licence: AcceptanceRecord | None
    remote_management: AcceptanceRecord | None
    publication: PublicationChoice | None

    @property
    def may_stream(self) -> bool:
        """Whether detections are permitted at all.

        The spec is specific that only ``licence`` gates streaming. In practice
        streaming also requires a token, and registration requires all three —
        so this is the narrower of two gates rather than the operative one.
        """
        return self.licence is not None

    @property
    def complete(self) -> bool:
        """Whether a ``RegisterRequest`` can be built.

        ``Agreements`` requires all three, so a node missing any of them cannot
        register, and therefore cannot do anything else either.
        """
        return all(
            record is not None
            for record in (self.licence, self.remote_management, self.publication)
        )

    @property
    def missing(self) -> list[str]:
        """Which records are absent, for the status document."""
        return [
            name
            for name, record in (
                (LICENCE, self.licence),
                (REMOTE_MANAGEMENT, self.remote_management),
                (PUBLICATION, self.publication),
            )
            if record is None
        ]


#: The fail-closed result, used for every absent or unreadable record.
NONE_GIVEN = Consent(licence=None, remote_management=None, publication=None)


def read_consent(path: Path | str = DEFAULT_CONSENT_PATH) -> Consent:
    """Read the consent records.

    Returns:
        Whatever the file records. Any record that is missing or malformed comes
        back as ``None`` rather than being guessed at, and an unreadable file
        yields :data:`NONE_GIVEN`. Never raises.
    """
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The ordinary state before anyone has been through the wizard.
        return NONE_GIVEN
    except OSError as exc:
        log.warning("consent records at %s could not be read: %s", path, exc)
        return NONE_GIVEN

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log.warning("consent records at %s are not valid JSON: %s", path, exc)
        return NONE_GIVEN

    if not isinstance(payload, dict):
        log.warning("consent records at %s are not a JSON object", path)
        return NONE_GIVEN

    return Consent(
        licence=_acceptance(payload.get(LICENCE), LICENCE, path),
        remote_management=_acceptance(payload.get(REMOTE_MANAGEMENT), REMOTE_MANAGEMENT, path),
        publication=_publication(payload.get(PUBLICATION), path),
    )


def _fields(value: Any, name: str, path: Path) -> tuple[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        log.warning("%s in %s is not an object", name, path)
        return None

    version = value.get("version")
    accepted_at = value.get("accepted_at")
    if not isinstance(version, str) or not version:
        log.warning("%s in %s has no version", name, path)
        return None
    if not isinstance(accepted_at, str) or not accepted_at:
        log.warning("%s in %s has no accepted_at", name, path)
        return None
    return version, accepted_at


def _acceptance(value: Any, name: str, path: Path) -> AcceptanceRecord | None:
    fields = _fields(value, name, path)
    return AcceptanceRecord(*fields) if fields else None


def _publication(value: Any, path: Path) -> PublicationChoice | None:
    fields = _fields(value, PUBLICATION, path)
    if fields is None:
        return None

    choice = value.get("choice")
    if choice not in ("public", "private"):
        # Not defaulted. The server treats an absent record as public, which is
        # its own decision about its own archive; inventing a choice here would
        # claim the owner made one.
        log.warning("publication in %s has no valid choice, got %r", path, choice)
        return None

    return PublicationChoice(*fields, choice=choice)
