"""Stage 2 — construction.

The only layer that knows both sides. Stage 1 hands over what the node
produced, in the node's units; this turns it into the spec's payloads, in the
spec's units. Stage 3 then sends them without knowing what any of it means.

Because it is a passthrough, **every field must be traceable to exactly one
source**. Two rules enforce that:

1. Builders take stage 1's own types in their signatures — ``DetectionPoll``,
   ``NodeConfigRaw``, ``HostSnapshot``, ``Consent`` — never dictionaries. If a
   builder needs something stage 1 does not collect, it is a keyword-only
   argument whose docstring names the module that will supply it.
2. Every builder's docstring carries the field map. If a wire field is not in
   one of those tables, nothing produces it.

``models.py`` is generated from ``docs/node-ingest-v1.yml`` — see
``tools/generate-models.sh``. It is not edited by hand, so the spec's own
constraints do the validating: ``node_id="Unknown"`` and a ``config_version``
of ``0`` are both rejected at construction without anyone remembering to check.

## Complete provenance

### DetectionFrame — ``detection.build_detection_frame``

| Wire field | Source | Conversion |
|---|---|---|
| ``t`` | ``DetectionPoll.timestamp_ms`` | ÷ 1000 → float seconds |
| ``seq`` | caller (``state.py``) |
| ``boot_id`` | caller (``state.py``) — distinct per process start | — |
| ``config_version`` | caller (``state.py``, server-issued) | — |
| ``delay`` | ``DetectionPoll.delay_km`` | × 3.335641 → µs |
| ``doppler`` | ``DetectionPoll.doppler_hz`` | none |
| ``snr`` | ``DetectionPoll.snr_db`` | none |
| ``adsb_hex`` | ``DetectionPoll.adsb`` | ``.hex`` per entry, or ``[None] * n`` |

Every array is truncated to the spec's ``maxItems`` of 512, and an ``adsb_hex``
entry failing ``^[0-9a-f]{6}$`` becomes ``null``. Both are the same trade: one
bad value must not cost the other detections in the frame.

### NodeConfig — ``config.build_node_config``

| Wire field | Source | Conversion |
|---|---|---|
| ``rx_lat`` / ``rx_lon`` | ``NodeConfigRaw.rx_lat`` / ``rx_lon`` | none |
| ``rx_alt_ft`` | ``NodeConfigRaw.rx_alt_m`` | × 3.28084 |
| ``tx_lat`` / ``tx_lon`` | ``NodeConfigRaw.tx_lat`` / ``tx_lon`` | none |
| ``tx_alt_ft`` | ``NodeConfigRaw.tx_alt_m`` | × 3.28084 |
| ``tx_callsign`` | ``NodeConfigRaw.tx_name`` | none — a display name, Q5 |
| ``fc_hz`` / ``fs_hz`` | ``NodeConfigRaw.fc_hz`` / ``fs_hz`` | none |
| ``max_range_km`` | ``delay_max_bins`` and ``fs_hz`` | × c ÷ fs ÷ 1000 |
| ``beam_width_deg`` | ``NodeConfigRaw.beam_width_deg`` | none — ``null`` if unset |
| ``beam_azimuth_deg`` | ``NodeConfigRaw.beam_azimuth_deg`` | none — ``null`` if unset |
| ``cpi_s`` | ``NodeConfigRaw.cpi_s`` | none, seconds both sides |
| ``delay_tolerance_us`` | ``NodeConfigRaw.delay_tolerance_km`` | × 3.335641 |
| ``doppler_tolerance_hz`` | ``NodeConfigRaw.doppler_tolerance_hz`` | none |

### RegisterRequest — ``registration.build_registration``

| Wire field | Source |
|---|---|
| ``node_id`` | ``identity.read_node_id()`` |
| ``board_model`` | ``identity.read_board_model()`` |
| ``agreements.licence`` | ``Consent.licence`` — **Q2, absent today** |
| ``agreements.remote_management`` | ``Consent.remote_management`` — **Q2** |
| ``agreements.publication`` | ``Consent.publication`` — **Q2** |
| ``config`` | ``build_node_config`` |

None of the three is ever synthesised. A missing record means the owner was not
shown that text, so the builder raises ``IncompletePayload`` naming which are
absent rather than producing a payload that would misrepresent them.

### HeartbeatRequest — ``heartbeat.build_heartbeat``

| Wire field | Source |
|---|---|
| ``state`` | caller (``comms/lifecycle.py``, stage 3b — already a wire value) |
| ``uptime_s`` | ``HostSnapshot.host_uptime_s`` — the **device's**, not ours |
| ``config_version`` | caller (``state.py``) — ``null`` until one is issued |
| ``health.cpu_pct`` | ``HostSnapshot.cpu_pct`` |
| ``health.disk_free_mb`` | ``HostSnapshot.disk_free_mb`` |
| ``health.temp_c`` | ``HostSnapshot.temp_c`` |
| ``health.blah2`` | ``Blah2Client.last_poll_ok`` → ``"up"`` / ``"down"`` / ``null`` |
| ``health.adsb`` | ``DetectionPoll.adsb is not None`` → ``"up"`` / omitted |
| ``versions.*`` | caller (compose env vars) |
| ``errors`` | caller (``errors.py``) |

Anything marked "caller" is not collected by stage 1 and cannot be. Those are
the seams where stage 3 and the state store plug in.

## Required nulls

Seven fields are *required and nullable* since spec v1.1.1 — both beam fields,
``HeartbeatRequest.config_version``, and ``NodeHealth``'s four core values. For
those, ``null`` is a value the server expects rather than an absence, so
payloads must go out through ``to_wire`` and never ``model_dump(exclude_none=
True)``, which would drop the key and produce something the server rejects.
"""

from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.detection import build_detection_frame
from retina_telemetry.wire.heartbeat import build_heartbeat
from retina_telemetry.wire.registration import IncompletePayload, build_registration
from retina_telemetry.wire.serialise import to_wire, to_wire_json

__all__ = [
    "IncompletePayload",
    "build_detection_frame",
    "build_heartbeat",
    "build_node_config",
    "build_registration",
    "to_wire",
    "to_wire_json",
]
