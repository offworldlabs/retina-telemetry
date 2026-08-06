"""Telemetry opt-in and the agreement acceptance record.

Telemetry is opt-in, via an explicit action in retina-gui's setup wizard. That
opt-in and the agreement acceptance the ingest spec requires are one artifact,
written by retina-gui and read-only to us:

.. code-block:: json

    {
      "opted_in": true,
      "agreement": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"}
    }

It sits beside retina-gui's other device state under ``/data/retina-gui``. Two
things are deliberately *not* copied from the neighbouring
``cloud-services-disabled`` flag: that is an empty file with negative sense,
which cannot carry ``{version, accepted_at}``, and for which "absent" is
indistinguishable from "the wizard never ran".

Nothing writes this file yet — the wizard's agreements step persists nothing
and the EULA is still a placeholder (open question Q2). That costs us nothing
here, because a missing file is a normal state meaning "not opted in".

Every failure path returns :data:`DENIED`. Consent is the one input where
failing closed is obviously right: if we cannot read what the owner agreed to,
we do not transmit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CONSENT_PATH = Path("/data/retina-gui/telemetry-consent.json")


@dataclass(frozen=True)
class Agreement:
    """What the owner accepted, and when.

    Both fields are passed through as stored. ``accepted_at`` is expected to be
    RFC 3339, but validating that is stage 2's job — this layer reports what
    the file says.
    """

    version: str
    accepted_at: str


@dataclass(frozen=True)
class Consent:
    opted_in: bool
    agreement: Agreement | None

    @property
    def may_transmit(self) -> bool:
        """Whether this node is permitted to talk to the server at all.

        Registration needs both halves: the spec requires an ``agreement``
        object, and the opt-in is what the owner actually chose.
        """
        return self.opted_in and self.agreement is not None


#: The fail-closed result, used for every absent or unreadable record.
DENIED = Consent(opted_in=False, agreement=None)


def read_consent(path: Path | str = DEFAULT_CONSENT_PATH) -> Consent:
    """Read the consent record.

    Returns:
        The record, or :data:`DENIED` if it is missing, unreadable, malformed,
        or does not carry the fields registration needs. Never raises.
    """
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The ordinary state before anyone has been through the wizard.
        return DENIED
    except OSError as exc:
        log.warning("consent record at %s could not be read: %s", path, exc)
        return DENIED

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        log.warning("consent record at %s is not valid JSON: %s", path, exc)
        return DENIED

    if not isinstance(payload, dict):
        log.warning("consent record at %s is not a JSON object", path)
        return DENIED

    opted_in = payload.get("opted_in")
    if not isinstance(opted_in, bool):
        log.warning("consent record at %s has no boolean opted_in", path)
        return DENIED

    agreement = _parse_agreement(payload.get("agreement"), path)
    if not opted_in:
        # Preserve the acceptance record even when telemetry is declined; the
        # two are separate facts and the owner may opt in later.
        return Consent(opted_in=False, agreement=agreement)

    return Consent(opted_in=True, agreement=agreement)


def _parse_agreement(value: object, path: Path) -> Agreement | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        log.warning("consent record at %s has a non-object agreement", path)
        return None

    version = value.get("version")
    accepted_at = value.get("accepted_at")
    if not isinstance(version, str) or not version:
        log.warning("consent record at %s has no agreement version", path)
        return None
    if not isinstance(accepted_at, str) or not accepted_at:
        log.warning("consent record at %s has no agreement accepted_at", path)
        return None

    return Agreement(version=version, accepted_at=accepted_at)
