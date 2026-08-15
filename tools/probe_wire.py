"""Run real node data through the stage 2 builders and print the wire payloads.

Run by ``tools/live-probe.sh``. Read-only: it collects exactly what the stage 1
probe collects, then converts. Nothing is sent anywhere — the payloads are
built and printed, never posted.

Unlike the collection probe, the *environment* is not what is under test here.
Stage 2 is a pure transform and behaves identically on a laptop. What this
proves is that **real** node values survive the conversions and produce
payloads the generated models accept — and it shows the exact JSON the server
would receive, which no unit test can, because the fixtures are ours and these
numbers are not.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retina_telemetry.collect import consent, host, identity, node_config  # noqa: E402
from retina_telemetry.collect.blah2 import Blah2Client  # noqa: E402
from retina_telemetry.wire.config import build_node_config  # noqa: E402
from retina_telemetry.wire.detection import build_detection_frame  # noqa: E402
from retina_telemetry.wire.heartbeat import build_heartbeat  # noqa: E402
from retina_telemetry.wire.registration import IncompletePayload, build_registration  # noqa: E402
from retina_telemetry.wire.serialise import to_wire
from retina_telemetry.wire.units import KM_TO_US, M_TO_FT  # noqa: E402
from tools.probe_report import (  # noqa: E402
    BOLD,
    RESET,
    check,
    detail,
    note,
    ok,
    probe,
    summarise,
)

# Placeholders for values stage 3 and state.py will own. Named loudly so no
# reader mistakes them for something this layer knows.
PLACEHOLDER_SEQ = 1
PLACEHOLDER_CONFIG_VERSION = 1
#: Real boot_ids come from state.py, which the probe does not construct. Shaped
#: to the spec's ^[0-9a-z]{8,32}$ so the payload printed here is one the server
#: would accept.
PLACEHOLDER_BOOT_ID = "probe000000000000"


def _json(model) -> None:
    # mode="json" is load-bearing: without it the acceptance timestamps stay as
    # datetime objects and json.dumps refuses them.
    detail(json.dumps(to_wire(model), indent=2))


# ── DetectionFrame ───────────────────────────────────────────────────


def probe_detection() -> None:
    client = Blah2Client()
    poll = None
    for _ in range(12):
        poll = client.poll_detection()
        if poll is not None:
            break
        time.sleep(0.25)
    client.close()

    if poll is None:
        note("no frame available", "blah2 may be down; skipping")
        return

    frame = build_detection_frame(
        poll,
        seq=PLACEHOLDER_SEQ,
        boot_id=PLACEHOLDER_BOOT_ID,
        config_version=PLACEHOLDER_CONFIG_VERSION,
    )

    ok("built from a live frame", f"{poll.n_detections} detection(s)")
    detail(
        f"collected  timestamp_ms={poll.timestamp_ms}  delay_km={poll.delay_km[:3]}\n"
        f"wire       t={frame.t}  delay={frame.delay[:3]}"
    )

    check(
        "t is the timestamp divided by 1000",
        abs(frame.t - poll.timestamp_ms / 1000.0) < 0.0005,
        f"{poll.timestamp_ms} → {frame.t}",
    )
    if poll.delay_km:
        expected = poll.delay_km[0] * KM_TO_US
        check(
            "delay multiplied by 3.335641, not divided",
            abs(frame.delay[0] - expected) < 0.01 and frame.delay[0] > poll.delay_km[0],
            f"{poll.delay_km[0]} km → {frame.delay[0]} µs",
        )
    else:
        note("empty frame", "valid and worth sending — 41 of 101 on Owl were empty")

    check(
        "doppler and snr are carried unchanged",
        frame.doppler == poll.doppler_hz and frame.snr == poll.snr_db,
        "already in the spec's units",
    )
    check(
        "all four arrays are the same length",
        len({len(frame.delay), len(frame.doppler), len(frame.snr), len(frame.adsb_hex)}) == 1,
        f"n={len(frame.delay)}",
    )
    if poll.adsb is None:
        note("adsb key absent", f"synthesised {frame.adsb_hex} — association is off")
    else:
        associated = [h for h in frame.adsb_hex if h is not None]
        note(f"{len(associated)}/{len(frame.adsb_hex)} associated", associated[:3] or "none")

    print(f"\n  {BOLD}what the server would receive:{RESET}")
    _json(frame)


# ── NodeConfig ───────────────────────────────────────────────────────


def probe_config() -> None:
    raw = node_config.read_config()

    # No longer refuses. Both beam fields are optional in the spec, and retina-gui
    # is not collecting the geometry from owners for the foreseeable future, so
    # every node in the fleet takes the "absent" path — this is the normal case
    # rather than a gap being worked around.
    wire = build_node_config(raw)
    ok("built from live config", "every field real, nothing substituted")

    if raw.beam_width_deg is None:
        note("beam geometry absent", "expected — both keys omitted from the payload")
    else:
        ok(f"beam_width_deg {wire.beam_width_deg}", "configured on this node")

    print(f"\n  {BOLD}what the server would receive:{RESET}")
    _json(wire)

    check(
        "rx altitude converted metres → feet",
        abs(wire.rx_alt_ft - raw.rx_alt_m * M_TO_FT) < 0.1,
        f"{raw.rx_alt_m} m → {wire.rx_alt_ft} ft",
    )
    check(
        "tx altitude converted metres → feet",
        wire.tx_alt_ft > raw.tx_alt_m,
        f"{raw.tx_alt_m} m → {wire.tx_alt_ft} ft",
    )
    check(
        "coordinates carried unchanged",
        (wire.rx_lat, wire.rx_lon) == (raw.rx_lat, raw.rx_lon),
        "degrees on both sides",
    )
    check(
        "max_range_km derived from bins and fs",
        wire.max_range_km > 0,
        f"{raw.delay_max_bins} bins @ {raw.fs_hz:.0f} Hz → {wire.max_range_km} km",
    )
    check(
        "tx_callsign carries the display name",
        wire.tx_callsign == raw.tx_name,
        f"{wire.tx_callsign!r} asks whether the server wants a real callsign",
    )


# ── HeartbeatRequest ─────────────────────────────────────────────────


def probe_heartbeat() -> None:
    reader = host.HostReader(disk_path="/data/mender")
    reader.read()  # prime the cpu sample
    time.sleep(1.0)
    snapshot = reader.read()

    client = Blah2Client()
    poll = client.poll_detection()
    client.close()

    if snapshot.host_uptime_s is None:
        note("host uptime unreadable", "caller would fall back to this process's uptime")
        return

    beat = build_heartbeat(
        state="streaming",
        uptime_s=snapshot.host_uptime_s,
        config_version=PLACEHOLDER_CONFIG_VERSION,
        boot_id=PLACEHOLDER_BOOT_ID,
        host=snapshot,
        blah2_up=client.last_poll_ok,
        adsb_present=poll.adsb is not None if poll else None,
        owl_os=None,
        retina_node=None,
        blah2_image=None,
    )

    ok("built from live host metrics")
    check(
        "uptime_s is the device's, not this process's",
        beat.uptime_s > 3600,
        f"{beat.uptime_s} s ≈ {beat.uptime_s / 86400:.1f} days — this container is seconds old",
    )
    check(
        "health carries what the host could read",
        beat.health is not None and beat.health.temp_c is not None,
        f"cpu {beat.health.cpu_pct}%  temp {beat.health.temp_c}°C  "
        f"disk {beat.health.disk_free_mb} MB",
    )
    # Compared on `.value`, which is not fussiness. `NodeHealth.blah2` is
    # required-and-nullable, so the generator gives its enum a `None` member and
    # cannot derive it from `str` — it is a plain `Enum`, where `Blah2.up ==
    # "up"` is False. `adsb` is optional, keeps no `None` member, and so comes
    # out a `StrEnum` that compares equal to its value. Two enums over the same
    # three words, differing only in nullability, and this check silently failed
    # against a perfectly good `"up"` from the day v1.1.1 was adopted.
    #
    # The payload is unaffected: `to_wire` dumps with `mode="json"`, which
    # renders either kind as its value.
    blah2 = beat.health.blah2
    check(
        "blah2 reported from the poll",
        getattr(blah2, "value", blah2) in ("up", "down", None),
        f"{blah2!r}",
    )
    note(
        f"adsb {beat.health.adsb!r}",
        "'up' or omitted, never 'down' — absent means association is switched off",
    )
    check(
        "queue_depth is gone from the schema",
        "queue_depth" not in type(beat.health).model_fields,
        "removed in v1.1.1 accepted",
    )
    note("versions omitted", "no readable source yet; needs the owl-os provides snapshot")

    print(f"\n  {BOLD}what the server would receive:{RESET}")
    _json(beat)


# ── RegisterRequest ──────────────────────────────────────────────────


def probe_registration() -> None:
    node_id = identity.read_node_id()
    board_model = identity.read_board_model()
    record = consent.read_consent()
    raw = node_config.read_config()

    try:
        build_registration(node_id=node_id, board_model=board_model, consent=record, config=raw)
        ok("built from live identity and consent", "both blockers have landed")
    except IncompletePayload as exc:
        note("refused, as expected", str(exc).split(".")[0])
        detail(str(exc))

        from retina_telemetry.collect.consent import (
            AcceptanceRecord,
            Consent,
            PublicationChoice,
        )

        simulated = build_registration(
            node_id=node_id,
            board_model=board_model,
            consent=Consent(
                licence=AcceptanceRecord("2026-07-01", "2026-07-31T09:12:00Z"),
                remote_management=AcceptanceRecord("2026-07-01", "2026-07-31T09:12:00Z"),
                publication=PublicationChoice("2026-07-01", "2026-07-31T09:12:00Z", "public"),
            ),
            config=raw,
        )
        print(f"\n  {BOLD}with a simulated consent record, everything else real:{RESET}")
        _json(simulated)

        check(
            "node_id is this node's, and passed the spec's pattern",
            simulated.node_id == node_id,
            node_id,
        )
        check(
            "board_model is the Mender device type",
            simulated.board_model == board_model,
            f"{board_model}, not the spec's 'raspberrypi5-4gb' example",
        )


def main() -> int:
    print(f"{BOLD}retina-telemetry — stage 2 live probe{RESET}")
    print(
        "\033[2mreal node data through the wire builders. "
        "nothing is sent — payloads are built and printed\033[0m"
    )

    probe("DetectionFrame", probe_detection)
    probe("NodeConfig", probe_config)
    probe("HeartbeatRequest", probe_heartbeat)
    probe("RegisterRequest", probe_registration)

    return summarise("stage 2")


if __name__ == "__main__":
    sys.exit(main())
