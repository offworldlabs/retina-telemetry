"""Two assumptions that let payloads go out via ``exclude_none=True``.

``wire/serialise.py`` used to own this. It existed for one field —
``NodeConfig.beam_azimuth_deg`` was *required* and *nullable*, so ``null`` was a
value the server expected rather than an absence, and ``exclude_none=True``
dropped the key and produced a payload the server rejects. The 2026-08-11
revision made both beam fields optional, which left the spec with no such field
and the module with nothing to do, so it went.

That trade is sound while two things stay true, and both fail silently if they
stop being true. Hence this file.
"""

from __future__ import annotations

import json
import typing

import pytest

from retina_telemetry.collect.host import HostSnapshot
from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.detection import build_detection_frame
from retina_telemetry.wire.heartbeat import build_heartbeat
from retina_telemetry.wire.models import (
    DetectionFrame,
    HeartbeatRequest,
    NodeConfig,
    RegisterRequest,
)
from retina_telemetry.wire.registration import build_registration
from tests.conftest import consented
from tests.wire.test_config import OWL
from tests.wire.test_detection import poll

PAYLOAD_SCHEMAS = (NodeConfig, RegisterRequest, DetectionFrame, HeartbeatRequest)


def _accepts_none(annotation: object) -> bool:
    """Whether the field itself is nullable.

    Deliberately not a string match on the annotation. ``adsb_hex`` is
    ``list[AdsbHexItem | None]`` — its *items* are nullable while the field is
    not, and items inside a list are never at risk from ``exclude_none``.
    """
    return type(None) in typing.get_args(annotation)


# ── assumption 1: nothing required is nullable ───────────────────────


def test_no_payload_field_is_both_required_and_nullable():
    """The assumption that makes ``exclude_none=True`` safe.

    If a spec revision adds a required-nullable field, ``exclude_none=True``
    will drop its ``null`` and the server will reject the payload — silently,
    because nothing else in the suite would notice. This is the canary. When it
    fires, either restore a serialiser that keeps required nulls (see git
    history for ``wire/serialise.py``) or handle that one field explicitly.
    """
    offenders = [
        f"{schema.__name__}.{name}"
        for schema in PAYLOAD_SCHEMAS
        for name, field in schema.model_fields.items()
        if field.is_required() and _accepts_none(field.annotation)
    ]

    assert offenders == [], (
        f"exclude_none=True will drop the null from {offenders}, which the spec "
        "requires. See this module's docstring."
    )


def test_adsb_hex_is_not_a_false_positive():
    """Pins the distinction the canary depends on. A list of nullable items is
    not a nullable field, and an earlier version of this check got that wrong."""
    field = DetectionFrame.model_fields["adsb_hex"]

    assert field.is_required()
    assert not _accepts_none(field.annotation)


# ── assumption 2: every payload survives json.dumps ──────────────────


def _payloads():
    yield "NodeConfig", build_node_config(OWL)
    yield (
        "RegisterRequest",
        build_registration(
            node_id="ret824685c9",
            board_model="pi5-v3-arm64",
            consent=consented(),
            config=OWL,
        ),
    )
    yield "DetectionFrame", build_detection_frame(poll(), seq=1, config_version=1)
    yield (
        "HeartbeatRequest",
        build_heartbeat(
            state="streaming",
            uptime_s=181569,
            config_version=1,
            host=HostSnapshot(cpu_pct=63.8, temp_c=70.5, disk_free_mb=15743, host_uptime_s=181569),
            blah2_up=True,
        ),
    )


@pytest.mark.parametrize("name,model", list(_payloads()), ids=lambda v: getattr(v, "__name__", v))
def test_every_payload_is_json_serialisable(name, model):
    """``mode="json"`` is load-bearing and easy to forget.

    Without it the acceptance timestamps stay as ``datetime`` objects and
    ``json.dumps`` refuses them outright — registration would fail at send time
    with nothing having validated it. There is no helper owning that argument
    any more, so this is what catches a call site that omits it.
    """
    json.dumps(model.model_dump(mode="json", exclude_none=True))


def test_omitting_mode_json_really_would_break_it():
    """Documents why the test above is not paranoia."""
    payload = build_registration(
        node_id="ret824685c9",
        board_model="pi5-v3-arm64",
        consent=consented(),
        config=OWL,
    )

    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps(payload.model_dump(exclude_none=True))


# ── what exclude_none actually does to our payloads ──────────────────


def test_unknown_health_fields_are_omitted():
    """The behaviour we do want: absence is the honest report for something we
    could not read."""
    partial = HostSnapshot(cpu_pct=None, temp_c=70.5, disk_free_mb=None, host_uptime_s=181569)

    payload = build_heartbeat(
        state="streaming", uptime_s=1, config_version=1, host=partial
    ).model_dump(mode="json", exclude_none=True)

    assert payload["health"] == {"temp_c": 70.5}


def test_empty_errors_list_is_kept_not_dropped():
    """An empty list is not None, so it survives — and it should. It reports
    that nothing has gone wrong since the last beat."""
    payload = build_heartbeat(state="starting", uptime_s=1, config_version=1).model_dump(
        mode="json", exclude_none=True
    )

    assert payload["errors"] == []


def test_required_fields_are_always_present():
    payload = build_heartbeat(state="starting", uptime_s=1, config_version=1).model_dump(
        mode="json", exclude_none=True
    )

    assert set(payload) >= {"state", "uptime_s", "config_version"}


def test_datetimes_become_strings():
    payload = build_registration(
        node_id="ret824685c9",
        board_model="pi5-v3-arm64",
        consent=consented(),
        config=OWL,
    ).model_dump(mode="json", exclude_none=True)

    assert isinstance(payload["agreements"]["licence"]["accepted_at"], str)
    assert payload["agreements"]["licence"]["accepted_at"].startswith("2026-07-31T09:12:00")
