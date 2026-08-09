import dataclasses

import pydantic
import pytest

from retina_telemetry.collect.consent import Agreement, Consent
from retina_telemetry.wire.registration import IncompletePayload, build_registration
from tests.wire.test_config import OWL

CONSENTED = Consent(
    opted_in=True,
    agreement=Agreement(version="2026-07-01", accepted_at="2026-07-31T09:12:00Z"),
)


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
    assert payload.agreement.version == "2026-07-01"  # consent.agreement
    assert payload.config.tx_callsign == "Crystal Palace"  # via build_node_config


def test_agreement_timestamp_is_parsed_not_passed_through():
    """Stage 1 stores it as written; the generated model parses it, so a bad
    timestamp fails here rather than being sent."""
    payload = build()

    assert payload.agreement.accepted_at.tzinfo is not None


def test_naive_timestamp_is_rejected():
    naive = Consent(opted_in=True, agreement=Agreement(version="1", accepted_at="not a date"))

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


def test_not_opted_in_refuses_to_build():
    with pytest.raises(IncompletePayload, match="not opted in"):
        build(consent=Consent(opted_in=False, agreement=CONSENTED.agreement))


def test_opted_in_without_an_agreement_refuses_to_build():
    with pytest.raises(IncompletePayload, match="agreement"):
        build(consent=Consent(opted_in=True, agreement=None))


def test_the_default_state_of_every_node_today_refuses_to_build():
    """Nothing writes the consent record yet, so this is what a real node
    currently produces."""
    from retina_telemetry.collect.consent import DENIED

    with pytest.raises(IncompletePayload, match="Q2"):
        build(consent=DENIED)


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
