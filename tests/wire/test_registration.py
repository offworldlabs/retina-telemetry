import dataclasses

import pydantic
import pytest

from retina_telemetry.collect.consent import AcceptanceRecord, Consent
from retina_telemetry.wire.registration import IncompletePayload, build_registration
from tests.conftest import consented
from tests.wire.test_config import OWL

CONSENTED = consented()


def build(**overrides):
    return build_registration(
        **{
            "node_id": "ret824685c9",
            "board_model": "pi5-v3-arm64",
            "consent": CONSENTED,
            "config": OWL,
            **overrides,
        }
    )


def test_every_field_traced_to_its_source():
    payload = build()

    assert payload.node_id == "ret824685c9"  # identity.read_node_id
    assert payload.board_model == "pi5-v3-arm64"  # identity.read_board_model
    assert payload.agreements.licence.version == "2026-07-01"  # consent.licence
    assert payload.config.tx_callsign == "Crystal Palace"  # via build_node_config


def test_all_three_records_reach_the_payload():
    payload = build()

    assert payload.agreements.licence.version == "2026-07-01"
    assert payload.agreements.remote_management.version == "2026-07-01"
    assert payload.agreements.publication.choice.value == "public"


def test_the_publication_choice_is_carried_verbatim():
    from retina_telemetry.collect.consent import PublicationChoice

    payload = build(
        consent=consented(
            publication=PublicationChoice("2026-07-01", "2026-07-31T09:12:00Z", "private")
        )
    )

    assert payload.agreements.publication.choice.value == "private"


def test_agreement_timestamps_are_parsed_not_passed_through():
    """Stage 1 stores them as written; the generated models parse them, so a bad
    timestamp fails here rather than being sent."""
    payload = build()

    assert payload.agreements.licence.accepted_at.tzinfo is not None


def test_a_naive_timestamp_is_rejected():
    naive = consented(licence=AcceptanceRecord("1", "not a date"))

    with pytest.raises(pydantic.ValidationError):
        build(consent=naive)


# ── the spec's own validation ────────────────────────────────────────


def test_unknown_can_never_reach_the_wire():
    """retina-gui's get_node_id() returns this string on failure. identity.py
    already refuses to emit it; the generated model refuses to carry it, from
    the spec's own pattern."""
    with pytest.raises(pydantic.ValidationError, match="ret"):
        build(node_id="Unknown")


def test_the_default_yml_placeholder_is_rejected():
    with pytest.raises(pydantic.ValidationError):
        build(node_id="ret000000000")


# ── consent gating ───────────────────────────────────────────────────


def test_a_missing_licence_refuses_to_build():
    with pytest.raises(IncompletePayload, match="licence"):
        build(consent=consented(licence=None))


def test_a_missing_remote_management_record_refuses_to_build():
    with pytest.raises(IncompletePayload, match="remote_management"):
        build(consent=consented(remote_management=None))


def test_a_missing_publication_choice_refuses_to_build():
    """It governs whether a dwelling's position reaches a public archive, so it
    is the last record that should ever be assumed."""
    with pytest.raises(IncompletePayload, match="publication"):
        build(consent=consented(publication=None))


def test_no_consent_record_is_ever_manufactured():
    """A missing record means the owner was not shown that text."""
    with pytest.raises(IncompletePayload, match="never shown"):
        build(consent=Consent(licence=None, remote_management=None, publication=None))


def test_the_default_state_of_every_node_today_refuses_to_build():
    """Nothing writes the consent records yet, so this is what a real node
    currently produces."""
    from retina_telemetry.collect.consent import NONE_GIVEN

    with pytest.raises(IncompletePayload, match="Q2"):
        build(consent=NONE_GIVEN)


# ── the Q1 blocker surfaces as one exception type ────────────────────


def test_missing_beam_width_surfaces_as_incomplete_payload():
    """IncompleteConfig is wrapped so a caller has one thing to catch."""
    with pytest.raises(IncompletePayload, match="Q1"):
        build(config=dataclasses.replace(OWL, beam_width_deg=None))


def test_incomplete_is_distinct_from_a_server_refusal():
    """A 403 means retry; this means there is nothing to retry with until
    something changes locally."""
    with pytest.raises(IncompletePayload):
        build(config=dataclasses.replace(OWL, beam_width_deg=None))


# ── board_model is diagnostic only ───────────────────────────────────


def test_unreadable_board_model_does_not_strand_the_node():
    """Required by the spec but diagnostic only — losing it must not block
    registration."""
    payload = build(board_model=None)

    assert payload.board_model == "unknown"


def test_board_model_is_the_mender_device_type_not_the_spec_example():
    """Their example is "raspberrypi5-4gb"; we send the Mender device type. See
    Q15 — the field is free text so nothing breaks."""
    assert build().board_model == "pi5-v3-arm64"
