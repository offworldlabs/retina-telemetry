"""The one rule: drop optional Nones, keep required ones.

Found by the live probe rather than by unit testing — printing a real payload
with `exclude_none=True` silently omitted a required field, which no assertion
in this repo was watching for.
"""

import dataclasses

from retina_telemetry.collect.consent import Agreement, Consent
from retina_telemetry.collect.host import HostSnapshot
from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.heartbeat import build_heartbeat
from retina_telemetry.wire.models import NodeConfig
from retina_telemetry.wire.registration import build_registration
from retina_telemetry.wire.serialise import to_wire, to_wire_json
from tests.wire.test_config import OWL

# ── the field that started this ──────────────────────────────────────


def test_null_beam_azimuth_survives_serialisation():
    """Required AND nullable — null is how the spec spells omnidirectional, so
    the key must be present. exclude_none=True would drop it and the server
    would reject the payload."""
    payload = to_wire(build_node_config(OWL))

    assert "beam_azimuth_deg" in payload
    assert payload["beam_azimuth_deg"] is None


def test_it_is_the_only_required_nullable_field_in_the_spec():
    """If a spec revision adds another, this catches it — the rule is derived
    from the models, but the surprise is worth surfacing."""
    nullable_required = [
        name
        for name, field in NodeConfig.model_fields.items()
        if field.is_required() and "None" in str(field.annotation)
    ]

    assert nullable_required == ["beam_azimuth_deg"]


def test_the_naive_approach_would_have_broken_it():
    """Documents why to_wire exists at all."""
    config = build_node_config(OWL)

    assert "beam_azimuth_deg" not in config.model_dump(exclude_none=True)
    assert "beam_azimuth_deg" in to_wire(config)


def test_nested_config_inside_registration_keeps_it():
    """The mistake propagates: NodeConfig is nested in RegisterRequest."""
    payload = to_wire(
        build_registration(
            node_id="ret824685c9",
            board_model="pi5-v3-arm64",
            consent=Consent(
                opted_in=True,
                agreement=Agreement(version="2026-07-01", accepted_at="2026-07-31T09:12:00Z"),
            ),
            config=OWL,
        )
    )

    assert "beam_azimuth_deg" in payload["config"]
    assert payload["config"]["beam_azimuth_deg"] is None


def test_a_real_azimuth_is_carried():
    payload = to_wire(build_node_config(dataclasses.replace(OWL, beam_azimuth_deg=135.5)))

    assert payload["beam_azimuth_deg"] == 135.5


# ── optional Nones are still dropped ─────────────────────────────────


def test_unknown_health_fields_are_omitted():
    """These are genuinely optional, so absence is the honest report."""
    partial = HostSnapshot(cpu_pct=None, temp_c=70.5, disk_free_mb=None, host_uptime_s=181569)

    payload = to_wire(
        build_heartbeat(state="streaming", uptime_s=1, config_version=1, host=partial)
    )

    assert payload["health"] == {"temp_c": 70.5}


def test_absent_health_block_is_omitted_entirely():
    payload = to_wire(build_heartbeat(state="idle", uptime_s=1, config_version=1))

    assert "health" not in payload
    assert "versions" not in payload


def test_required_fields_are_always_present():
    payload = to_wire(build_heartbeat(state="idle", uptime_s=1, config_version=1))

    assert set(payload) >= {"state", "uptime_s", "config_version"}


def test_empty_errors_list_is_kept_not_dropped():
    """It is optional but not None — an empty list is a meaningful report that
    nothing has gone wrong since the last beat."""
    payload = to_wire(build_heartbeat(state="idle", uptime_s=1, config_version=1))

    assert payload["errors"] == []


# ── encoding ─────────────────────────────────────────────────────────


def test_datetimes_become_strings_not_objects():
    payload = to_wire(
        build_registration(
            node_id="ret824685c9",
            board_model="pi5-v3-arm64",
            consent=Consent(
                opted_in=True,
                agreement=Agreement(version="2026-07-01", accepted_at="2026-07-31T09:12:00Z"),
            ),
            config=OWL,
        )
    )

    assert isinstance(payload["agreement"]["accepted_at"], str)
    assert payload["agreement"]["accepted_at"].startswith("2026-07-31T09:12:00")


def test_json_round_trips():
    import json

    text = to_wire_json(build_node_config(OWL))

    assert json.loads(text)["beam_azimuth_deg"] is None
