"""Exercise every stage 1 module against a real node, from inside a container.

Run by ``tools/live-probe.sh``. Read-only throughout: it opens files, polls
``/api/detection``, and writes nothing anywhere.

The container matters. Three of ``host.py``'s claims are only testable inside
one — ``/proc`` not being namespaced, ``statvfs`` being namespaced, and
``/proc/uptime`` reporting the host rather than this process — and a host-side
run would pass them trivially while proving nothing.

Exit code is the number of checks that failed, so the shell wrapper can tell
success from failure without parsing the output.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retina_telemetry.collect import consent, host, identity, node_config  # noqa: E402
from retina_telemetry.collect.blah2 import Blah2Client  # noqa: E402
from tools.probe_report import (  # noqa: E402
    BOLD,
    DIM,
    RESET,
    bad,
    check,
    note,
    ok,
    probe,
    section,
    summarise,
)

# ── identity ─────────────────────────────────────────────────────────


def probe_identity() -> None:
    node_id = identity.read_node_id()
    check(
        f"node_id  {BOLD}{node_id}{RESET}",
        bool(identity.NODE_ID_PATTERN.match(node_id)),
        f"matches {identity.NODE_ID_PATTERN.pattern}",
    )
    check("is not the 'Unknown' retina-gui returns on failure", node_id != "Unknown")
    check("is not the default.yml placeholder", node_id != "ret000000000")

    board = identity.read_board_model()
    check(f"board_model  {BOLD}{board}{RESET}", board is not None, "device_type= prefix stripped")


# ── node config ──────────────────────────────────────────────────────


def probe_node_config() -> None:
    config = node_config.read_config()

    ok("read and validated", f"{len(vars(config))} fields")
    print(
        f"    {DIM}rx {config.rx_lat}, {config.rx_lon} @ {config.rx_alt_m} m\n"
        f"    tx {config.tx_lat}, {config.tx_lon} @ {config.tx_alt_m} m "
        f"({config.tx_name!r})\n"
        f"    fc {config.fc_hz:.0f} Hz   fs {config.fs_hz:.0f} Hz   "
        f"delayMax {config.delay_max_bins} bins{RESET}"
    )

    check(
        "altitudes still in metres, not feet",
        config.rx_alt_m < 1000,
        f"rx_alt_m={config.rx_alt_m} (×3.28084 in stage 2)",
    )
    check(
        "delay_max is bins, not km",
        isinstance(config.delay_max_bins, int),
        f"{config.delay_max_bins} → max_range_km derived in stage 2",
    )

    # Q1: expected to be absent until retina-gui writes them.
    if config.beam_width_deg is None:
        note("beam_width_deg absent", "expected — Q1, blocks registration")
    else:
        ok(f"beam_width_deg {config.beam_width_deg}", "Q1 has landed")
    note(
        f"beam_azimuth_deg {config.beam_azimuth_deg}",
        "None is valid — means omnidirectional",
    )

    again = node_config.read_config()
    check("re-read compares equal (change detection)", config == again, "frozen dataclass ==")


# ── consent ──────────────────────────────────────────────────────────


def probe_consent() -> None:
    record = consent.read_consent()
    ok(f"read without raising  {BOLD}complete={record.complete}{RESET}")

    if record == consent.NONE_GIVEN:
        note(f"missing: {record.missing}", "expected — nothing writes them yet (Q2)")
    check(
        "may_stream agrees with the licence record",
        record.may_stream == (record.licence is not None),
        f"may_stream={record.may_stream}",
    )

    path = consent.DEFAULT_CONSENT_PATH
    note(f"{path}", "exists" if path.exists() else "absent, which is a normal state")


# ── blah2 ────────────────────────────────────────────────────────────


def probe_blah2() -> None:
    client = Blah2Client()
    check(
        "no poll yet → last_poll_ok is None", client.last_poll_ok is None, "stage 2 omits the field"
    )

    frame = None
    for _ in range(12):  # a frame lands roughly every 0.9 s on this fleet
        frame = client.poll_detection()
        if frame is not None:
            break
        time.sleep(0.25)

    if frame is None:
        bad("no frame in 3 s", f"last_poll_ok={client.last_poll_ok} last_error={client.last_error}")
        return

    check("polled a frame", True, f"timestamp_ms={frame.timestamp_ms}")
    check("last_poll_ok is True", client.last_poll_ok is True, "→ NodeHealth.blah2 'up'")
    check(
        "timestamp is epoch milliseconds, not seconds",
        frame.timestamp_ms > 1_000_000_000_000,
        f"{frame.timestamp_ms} — ÷1000 in stage 2",
    )
    check(
        "arrays are parallel and equal length",
        len(frame.delay_km) == len(frame.doppler_hz) == len(frame.snr_db),
        f"n={frame.n_detections}",
    )
    if frame.delay_km:
        print(
            f"    {DIM}delay_km {frame.delay_km[:3]}  doppler_hz {frame.doppler_hz[:3]}\n"
            f"    snr_db   {frame.snr_db[:3]}{RESET}"
        )
    else:
        note("empty frame", "normal — detector running, nothing detected")

    if frame.adsb is None:
        note("adsb key absent", "→ ADS-B disabled, NodeHealth.adsb omitted")
    else:
        check(
            "adsb array parallel to the others",
            len(frame.adsb) == frame.n_detections,
            f"{sum(a is not None for a in frame.adsb)}/{len(frame.adsb)} associated",
        )
        populated = next((a for a in frame.adsb if a is not None), None)
        if populated is None:
            note("no populated association seen", "the one fixture still unverified live")
        else:
            ok("association object observed", f"hex={populated.get('hex')}")
            print(f"    {DIM}{sorted(populated)}{RESET}")

    # Dedupe: an immediate re-poll is inside the same CPI.
    check("immediate re-poll deduped", client.poll_detection() is None, "same timestamp")
    client.close()


# ── host ─────────────────────────────────────────────────────────────


def probe_host() -> None:
    import os

    reader = host.HostReader(disk_path="/data/mender")
    first = reader.read()
    check("first read primes cpu, returns None", first.cpu_pct is None, "/proc/stat is cumulative")

    time.sleep(1.0)
    snap = reader.read()

    check("cpu_pct on the second read", snap.cpu_pct is not None, f"{snap.cpu_pct}%")
    check(
        "temp_c plausible for a Pi under load",
        snap.temp_c is not None and 20 < snap.temp_c < 90,
        f"{snap.temp_c} °C",
    )
    check("host_uptime_s read", snap.host_uptime_s is not None, f"{snap.host_uptime_s} s")

    section("host.py's container claims")

    # /proc is not namespaced: this container is seconds old, so anything much
    # larger can only be the host's.
    check(
        "/proc/uptime is the HOST's, not this container's",
        snap.host_uptime_s is not None and snap.host_uptime_s > 3600,
        f"{snap.host_uptime_s} s ≈ {snap.host_uptime_s / 86400:.1f} days",
    )

    # statvfs IS namespaced by mount, so `/` is the container's overlay. Whether
    # that differs from /data depends on where Docker keeps its data-root, so
    # compare devices rather than asserting the numbers must differ.
    # Note this is reported, not asserted. overlay2 passes statvfs through to
    # its backing store, so whether `/` and `/data` agree depends entirely on
    # where Docker keeps its data-root — and both outcomes are informative.
    # (st_dev is no help: the overlay always has its own device number.)
    overlay = os.statvfs("/")
    overlay_mb = int(overlay.f_bavail * overlay.f_frsize / 1_000_000)

    if abs(overlay_mb - (snap.disk_free_mb or 0)) <= 100:
        note(
            f"/ and /data agree — both {overlay_mb} MB",
            "the overlay is backed by the /data filesystem (data-root is /data/docker)",
        )
        note(
            "so statvfs('/') is accidentally right on this fleet",
            "still wrong in principle — move the data-root and it silently diverges",
        )
    else:
        note(
            f"/data {snap.disk_free_mb} MB   vs   / {overlay_mb} MB",
            "the overlay is on its own store, so statvfs('/') would be meaningless",
        )

    check(
        "disk_free_mb reports the node's storage",
        snap.disk_free_mb is not None and snap.disk_free_mb > 0,
        f"{snap.disk_free_mb} MB free on /data",
    )


def main() -> int:
    print(f"{BOLD}retina-telemetry — stage 1 live probe{RESET}")
    print(f"{DIM}read-only; polls /api/detection and reads mounted files{RESET}")

    probe("identity.py", probe_identity)
    probe("node_config.py", probe_node_config)
    probe("consent.py", probe_consent)
    probe("blah2.py", probe_blah2)
    probe("host.py", probe_host)

    return summarise("stage 1")


if __name__ == "__main__":
    sys.exit(main())
