import dataclasses

import pydantic
import pytest

from retina_telemetry.collect.consent import AcceptanceRecord, Consent
from retina_telemetry.wire.registration import (
    IncompletePayload,
    UnsupportedNodeId,
    build_registration,
    spec_accepts_node_id,
    spec_node_id_pattern,
)
from retina_telemetry.wire.serialise import to_wire
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

    with pytest.raises(IncompletePayload, match="never shown"):
        build(consent=NONE_GIVEN)


# ── an uncharacterised antenna does not block ────────────────────────


def test_a_node_without_beam_geometry_still_registers():
    """The normal case for every node in the fleet: retina-gui does not collect
    the geometry, so registration must not depend on it."""
    payload = to_wire(build(config=dataclasses.replace(OWL, beam_width_deg=None)))

    assert payload["config"]["beam_width_deg"] is None
    assert payload["config"]["rx_lat"] == 51.4769


def test_incomplete_is_distinct_from_a_server_refusal():
    """A 403 means retry; this means there is nothing to retry with until
    something changes locally. Consent is the only trigger now — the beam fields
    stopped being one when the spec made them nullable."""
    from retina_telemetry.collect.consent import NONE_GIVEN

    with pytest.raises(IncompletePayload):
        build(consent=NONE_GIVEN)


# ── board_model is diagnostic only ───────────────────────────────────


def test_unreadable_board_model_does_not_strand_the_node():
    """Required by the spec but diagnostic only — losing it must not block
    registration."""
    payload = build(board_model=None)

    assert payload.board_model == "unknown"


def test_board_model_is_the_mender_device_type_not_the_spec_example():
    """Their example is "raspberrypi5-4gb"; we send the Mender device type,
    which is what decides the software a board may receive. The field is free
    text in the schema, so nothing breaks."""
    assert build().board_model == "pi5-v3-arm64"


# ── the registration gate ────────────────────────────────────────────
#
# A migrated node is deliberately held back from registering while the ingest
# spec still pins the legacy format. Held back, not broken: it keeps running and
# says why. These tests describe a gate that is *meant* to open on its own, so
# they are written against the spec's own pattern rather than against a literal.


def test_the_gate_reads_the_pattern_out_of_the_generated_model():
    """Not a second copy of it. wire/models.py is generated from the contract,
    so regenerating it against a newer spec lifts this gate with no code change
    here — which is the entire point of not writing the pattern down twice."""
    assert spec_node_id_pattern() == "^ret[0-9a-f]{8}$"


def test_an_id_the_spec_carries_is_accepted():
    assert spec_accepts_node_id("ret824685c9")


def test_a_migrated_id_is_refused_while_the_spec_predates_it():
    assert not spec_accepts_node_id("retgec420d03ea4b064")


def test_building_a_registration_for_a_migrated_node_raises():
    with pytest.raises(UnsupportedNodeId, match="ingest spec still requires"):
        build(node_id="retgec420d03ea4b064")


def test_the_refusal_names_the_spec_rather_than_the_node():
    """The operator must not go looking for a fault on the board. There is not
    one: the node is healthy and the contract is behind it."""
    with pytest.raises(UnsupportedNodeId) as caught:
        build(node_id="retgec420d03ea4b064")

    message = str(caught.value)
    assert "Nothing on the node will resolve this" in message
    assert "wire/models.py" in message


def test_it_is_an_incomplete_payload_so_existing_handlers_still_catch_it():
    with pytest.raises(IncompletePayload):
        build(node_id="retgec420d03ea4b064")


def test_something_that_is_not_a_node_id_is_not_treated_as_migrated():
    """A gate that claimed "Unknown" was a migrated node would send whoever
    read it hunting for a migration that never happened. Garbage is the
    generated model's business, and its answer is the honest one."""
    with pytest.raises(pydantic.ValidationError):
        build(node_id="Unknown")

    with pytest.raises(pydantic.ValidationError):
        build(node_id="ret000000000")


def test_the_gate_is_checked_before_consent():
    """Otherwise a migrated node with no agreements tells its owner to finish
    the wizard, which cannot unblock them."""
    with pytest.raises(UnsupportedNodeId):
        build(node_id="retgec420d03ea4b064", consent=Consent(None, None, None))
