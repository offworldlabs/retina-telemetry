"""The one rule: drop optional Nones, keep required ones.

Deleted on 2026-08-11 when the spec briefly had no required-nullable field, and
restored with v1.1.1, which has seven. The deletion is worth remembering: the
rule was correct the whole time, and what changed was only whether anything
exercised it. That is the failure mode this module guards — a serialiser
everyone assumes is fine, producing payloads the server rejects.
"""

from __future__ import annotations

import dataclasses
import json
import typing

import pytest
from pydantic import BaseModel

from retina_telemetry.collect.host import HostSnapshot
from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.heartbeat import build_heartbeat
from retina_telemetry.wire.models import (
    DetectionFrame,
    HeartbeatRequest,
    NodeConfig,
    NodeHealth,
    RegisterRequest,
)
from retina_telemetry.wire.registration import build_registration
from retina_telemetry.wire.serialise import to_wire, to_wire_json
from tests.conftest import consented
from tests.wire.test_config import OWL

PAYLOAD_SCHEMAS = (NodeConfig, RegisterRequest, DetectionFrame, HeartbeatRequest, NodeHealth)

#: Every field the spec declares required *and* nullable. `null` on these is a
#: value the server expects, not an absence, so dropping the key produces a
#: payload it rejects. Listed by hand and checked against the generated models
#: below, so a revision that adds or removes one is a visible edit here rather
#: than a silent change in behaviour.
REQUIRED_NULLABLE = {
    "NodeConfig.beam_width_deg",
    "NodeConfig.beam_azimuth_deg",
    "HeartbeatRequest.config_version",
    "NodeHealth.cpu_pct",
    "NodeHealth.disk_free_mb",
    "NodeHealth.temp_c",
    "NodeHealth.blah2",
}


def _accepts_none(annotation: object) -> bool:
    """Whether the field itself is nullable.

    Deliberately not a string match on the annotation. `adsb_hex` is
    `list[AdsbHexItem | None]` — its *items* are nullable while the field is
    not, and items inside a list are never at risk from `exclude_none`.
    """
    return type(None) in typing.get_args(annotation)


def _required_nullable() -> set[str]:
    return {
        f"{schema.__name__}.{name}"
        for schema in PAYLOAD_SCHEMAS
        for name, field in schema.model_fields.items()
        if field.is_required() and _accepts_none(field.annotation)
    }


# ── the inventory the rule exists for ────────────────────────────────


def test_the_spec_declares_exactly_the_fields_we_think_it_does():
    """The canary. A revision that adds a required-nullable field fails here,
    and whoever adopts it is pointed at the rule before shipping."""
    assert _required_nullable() == REQUIRED_NULLABLE


def test_adsb_hex_is_not_a_false_positive():
    """A list of nullable items is not a nullable field. An earlier version of
    the check got this wrong by matching on the annotation's text."""
    field = DetectionFrame.model_fields["adsb_hex"]

    assert field.is_required()
    assert not _accepts_none(field.annotation)


# ── required nulls survive ───────────────────────────────────────────


def test_a_heartbeat_without_a_config_version_keeps_the_null():
    """The Q16 case. A node that has never been issued a version must still
    beat, and the key carries `null` rather than vanishing."""
    payload = to_wire(
        build_heartbeat(state="starting", uptime_s=1, config_version=None, boot_id="k3n8v2qp71ab")
    )

    assert "config_version" in payload
    assert payload["config_version"] is None


def test_unknown_health_values_are_nulls_not_absences():
    """`/proc/stat` is cumulative, so the first beat after start genuinely has
    no cpu_pct. v1.1.1 wants that said, not left out."""
    partial = HostSnapshot(cpu_pct=None, temp_c=70.5, disk_free_mb=None, host_uptime_s=1)

    health = to_wire(
        build_heartbeat(
            state="streaming", uptime_s=1, config_version=1, boot_id="k3n8v2qp71ab", host=partial
        )
    )["health"]

    assert health["cpu_pct"] is None
    assert health["disk_free_mb"] is None
    assert health["temp_c"] == 70.5


def test_an_uncharacterised_antenna_sends_two_nulls():
    bare = dataclasses.replace(OWL, beam_width_deg=None, beam_azimuth_deg=None)

    payload = to_wire(build_node_config(bare))

    assert payload["beam_width_deg"] is None
    assert payload["beam_azimuth_deg"] is None


def test_the_rule_reaches_nested_models():
    """The case that bit originally: NodeConfig is nested inside
    RegisterRequest, so a naive serialiser breaks the one request a node cannot
    recover from failing."""
    bare = dataclasses.replace(OWL, beam_width_deg=None, beam_azimuth_deg=None)

    payload = to_wire(
        build_registration(
            node_id="ret824685c9", board_model="pi5-v3-arm64", consent=consented(), config=bare
        )
    )

    assert payload["config"]["beam_width_deg"] is None
    assert payload["config"]["beam_azimuth_deg"] is None


# ── optional Nones are still dropped ─────────────────────────────────


def test_adsb_is_omitted_when_association_is_off():
    """The one health field that stays optional, because its absence carries a
    third meaning: disabled, which the enum has no word for."""
    health = to_wire(
        build_heartbeat(
            state="streaming",
            uptime_s=1,
            config_version=1,
            boot_id="k3n8v2qp71ab",
            adsb_present=False,
        )
    )["health"]

    assert "adsb" not in health


def test_absent_versions_block_is_omitted_entirely():
    payload = to_wire(
        build_heartbeat(state="starting", uptime_s=1, config_version=1, boot_id="k3n8v2qp71ab")
    )

    assert "versions" not in payload


def test_empty_errors_list_is_kept_not_dropped():
    """Not None, so it survives — and it should. An empty list reports that
    nothing has gone wrong since the last beat."""
    payload = to_wire(
        build_heartbeat(state="starting", uptime_s=1, config_version=1, boot_id="k3n8v2qp71ab")
    )

    assert payload["errors"] == []


# ── why exclude_none is not enough ───────────────────────────────────


def test_exclude_none_would_drop_every_required_null():
    """Documents what this module is for, against the real payloads rather than
    a synthetic model."""
    beat = build_heartbeat(
        state="starting", uptime_s=1, config_version=None, boot_id="k3n8v2qp71ab"
    )

    naive = beat.model_dump(mode="json", exclude_none=True)

    assert "config_version" not in naive
    assert "cpu_pct" not in naive["health"]
    assert "config_version" in to_wire(beat)


def test_mode_json_is_load_bearing():
    """Without it the acceptance timestamps stay as datetime objects and
    json.dumps refuses the registration payload outright."""
    payload = build_registration(
        node_id="ret824685c9", board_model="pi5-v3-arm64", consent=consented(), config=OWL
    )

    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(payload.model_dump(exclude_none=True))

    json.dumps(to_wire(payload))  # to_wire does it for us


# ── encoding ─────────────────────────────────────────────────────────


def test_datetimes_become_strings():
    payload = to_wire(
        build_registration(
            node_id="ret824685c9", board_model="pi5-v3-arm64", consent=consented(), config=OWL
        )
    )

    assert isinstance(payload["agreements"]["licence"]["accepted_at"], str)
    assert payload["agreements"]["licence"]["accepted_at"].startswith("2026-07-31T09:12:00")


def test_json_round_trips():
    assert json.loads(to_wire_json(build_node_config(OWL)))["cpi_s"] == 0.5


def test_a_plain_required_none_is_impossible_to_construct():
    """Sanity check on the models themselves: a required non-nullable field
    cannot hold None, so the rule only ever has to consider nullable ones."""

    class Strict(BaseModel):
        value: float

    with pytest.raises(Exception):
        Strict(value=None)
