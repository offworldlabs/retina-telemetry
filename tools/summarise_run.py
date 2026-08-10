"""Summarise what the mock received during a live run.

Reads ``/_control/requests`` on stdin. Separate from the mock itself so the
mock stays a server rather than a reporting tool.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

BOLD, DIM, GREEN, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[0m"


def timeline(requests: list[dict]) -> None:
    """One line per second, showing what arrived. Gaps are the point.

    A detection stopping while heartbeats continue is what a revoked token
    looks like from the server's side, and it is far more legible as a shape
    than as a list of assertions.
    """
    from datetime import datetime

    start = datetime.fromisoformat(requests[0]["at"])
    buckets: dict[int, Counter] = {}
    for request in requests:
        second = int((datetime.fromisoformat(request["at"]) - start).total_seconds())
        buckets.setdefault(second, Counter())[request["endpoint"]] += 1

    glyph = {"detection": "·", "heartbeat": "H", "config": "C", "register": "R"}
    print(f"  {DIM}t     detections            other{RESET}")
    for second in range(max(buckets) + 1):
        counts = buckets.get(second, Counter())
        dots = glyph["detection"] * counts["detection"]
        other = "".join(glyph[name] * counts[name] for name in ("register", "config", "heartbeat"))
        marker = "" if counts else f"{DIM}  — nothing{RESET}"
        print(f"  {second:>3}   {dots:<20} {GREEN}{other}{RESET}{marker}")


def main() -> int:
    requests = json.load(sys.stdin)["requests"]
    if not requests:
        print("nothing received")
        return 1

    if "--timeline" in sys.argv:
        timeline(requests)
        print()

    counts = Counter(r["endpoint"] for r in requests)
    for endpoint in ("register", "config", "heartbeat", "detection"):
        if counts[endpoint]:
            print(f"  {GREEN}✓{RESET} {endpoint:10} {counts[endpoint]:>4}")

    detections = [r for r in requests if r["endpoint"] == "detection"]
    if detections:
        first, last = detections[0]["body"], detections[-1]["body"]
        span = last["t"] - first["t"]
        total = sum(len(r["body"]["delay"]) for r in detections)
        empty = sum(1 for r in detections if not r["body"]["delay"])
        gaps = last["seq"] - first["seq"] + 1 - len(detections)

        print(f"\n  {BOLD}detections{RESET}")
        print(f"    {DIM}window        {span:.1f} s, {len(detections)} frames sent{RESET}")
        print(f"    {DIM}seq           {first['seq']} → {last['seq']}, {gaps} dropped{RESET}")
        print(f"    {DIM}detections    {total} across all frames, {empty} empty frames{RESET}")
        print(f"    {DIM}config_ver    {last['config_version']}{RESET}")

        sample = next((r["body"] for r in detections if r["body"]["delay"]), None)
        if sample:
            print(f"\n  {BOLD}a real frame, converted{RESET}")
            print(f"    {DIM}t           {sample['t']}  (epoch seconds){RESET}")
            print(f"    {DIM}delay       {sample['delay'][:4]}  (microseconds){RESET}")
            print(f"    {DIM}doppler     {sample['doppler'][:4]}  (Hz){RESET}")
            print(f"    {DIM}snr         {sample['snr'][:4]}  (dB){RESET}")
            print(f"    {DIM}adsb_hex    {sample['adsb_hex'][:4]}{RESET}")

    beats = [r for r in requests if r["endpoint"] == "heartbeat"]
    if beats:
        last = beats[-1]["body"]
        print(f"\n  {BOLD}last heartbeat{RESET}")
        print(f"    {DIM}{json.dumps(last, indent=2)[:600]}{RESET}")

    registration = next((r for r in requests if r["endpoint"] == "register"), None)
    if registration:
        body = registration["body"]
        print(f"\n  {BOLD}registration{RESET}")
        print(f"    {DIM}node_id       {body['node_id']}{RESET}")
        print(f"    {DIM}board_model   {body['board_model']}{RESET}")
        print(f"    {DIM}rx_alt_ft     {body['config']['rx_alt_ft']}  (from metres){RESET}")
        print(f"    {DIM}max_range_km  {body['config']['max_range_km']}  (derived){RESET}")
        print(f"    {DIM}beam_azimuth  {body['config']['beam_azimuth_deg']}  (null = omni){RESET}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
