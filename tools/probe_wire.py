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

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retina_telemetry.collect import consent, host, identity, node_config  # noqa: E402
from retina_telemetry.collect.blah2 import Blah2Client  # noqa: E402
from retina_telemetry.wire.config import IncompleteConfig, build_node_config  # noqa: E402
from retina_telemetry.wire.detection import build_detection_frame  # noqa: E402
from retina_telemetry.wire.heartbeat import build_heartbeat  # noqa: E402
from retina_telemetry.wire.registration import IncompletePayload, build_registration  # noqa: E402
from retina_telemetry.wire.serialise import to_wire_json  # noqa: E402
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


def _json(model) -> None:
    # to_wire, not model_dump_json(exclude_none=True) — the latter drops
    # beam_azimuth_deg, which is required and nullable. This probe printing a
    # payload without it is how that was found.
    detail(to_wire_json(model, indent=2))


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
        poll, seq=PLACEHOLDER_SEQ, config_version=PLACEHOLDER_CONFIG_VERSION
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

    try:
        build_node_config(raw)
        ok("built from live config", "Q1 has landed — beam geometry is configured")
    except IncompleteConfig as exc:
        note("refused, as expected", "Q1 — beam_width_deg is not configured on this node")
        detail(str(exc))

        # Everything except that one field is real. Substituting a placeholder
        # shows the payload the server will actually get once retina-gui writes
        # it, and proves nothing else is missing.
        import dataclasses

        simulated = build_node_config(dataclasses.replace(raw, beam_width_deg=60.0))
        print(f"\n  {BOLD}with a placeholder beam_width_deg=60, everything else real:{RESET}")
        _json(simulated)

        check(
            "rx altitude converted metres → feet",
            abs(simulated.rx_alt_ft - raw.rx_alt_m * M_TO_FT) < 0.1,
            f"{raw.rx_alt_m} m → {simulated.rx_alt_ft} ft",
        )
        check(
            "tx altitude converted metres → feet",
            simulated.tx_alt_ft > raw.tx_alt_m,
            f"{raw.tx_alt_m} m → {simulated.tx_alt_ft} ft",
        )
        check(
            "coordinates carried unchanged",
            (simulated.rx_lat, simulated.rx_lon) == (raw.rx_lat, raw.rx_lon),
            "degrees on both sides",
        )
        check(
            "max_range_km derived from bins and fs",
            simulated.max_range_km > 0,
            f"{raw.delay_max_bins} bins @ {raw.fs_hz:.0f} Hz → {simulated.max_range_km} km",
        )
        check(
            "tx_callsign carries the display name",
            simulated.tx_callsign == raw.tx_name,
            f"{simulated.tx_callsign!r} — Q5 asks whether the server wants a real callsign",
        )
        note("beam_azimuth_deg", f"{simulated.beam_azimuth_deg} — null means omnidirectional")


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
    check(
        "blah2 reported from the poll",
        beat.health.blah2 in ("up", "down", None),
        f"{beat.health.blah2!r}",
    )
    note(
        f"adsb {beat.health.adsb!r}",
        "'up' or omitted, never 'down' — absent means association is switched off",
    )
    check("queue_depth never populated", beat.health.queue_depth is None, "meaningless — Q11")
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

        import dataclasses

        from retina_telemetry.collect.consent import Agreement, Consent

        simulated = build_registration(
            node_id=node_id,
            board_model=board_model,
            consent=Consent(
                opted_in=True,
                agreement=Agreement(version="2026-07-01", accepted_at="2026-07-31T09:12:00Z"),
            ),
            config=dataclasses.replace(raw, beam_width_deg=60.0),
        )
        print(
            f"\n  {BOLD}with a simulated consent record and beam width, "
            f"identity and config real:{RESET}"
        )
        _json(simulated)

        check(
            "node_id is this node's, and passed the spec's pattern",
            simulated.node_id == node_id,
            node_id,
        )
        check(
            "board_model is the Mender device type",
            simulated.board_model == board_model,
            f"{board_model} — Q15, not the spec's 'raspberrypi5-4gb' example",
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
