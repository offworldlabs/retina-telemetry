"""A stand-in for blah2-api's detection path, for trying a tracker build on a node.

blah2-api does two things with each frame this matters for: it stores it and
serves it at ``GET /api/detection``, and it forwards the very same string to
retina-tracker over TCP (``forwardToTracker`` in ``api/server.js``). This does
both, on ports of its own, so a second tracker can run beside the node's real
one and retina-telemetry can be pointed at the pair without touching the
running stack.

Two sources of frames:

    --source relay       blah2-api's own frames, polled from 127.0.0.1:3000 and
                         passed on unchanged. Real detections, when there are any.
    --source synthetic   made up here: two targets that move consistently with
                         the node's own centre frequency (so a Kalman filter can
                         follow them), one that appears and later stops so a
                         track is born and dies, and clutter. Detection order
                         is shuffled every frame, so a track's index moves.

Stdlib only, so it runs in any Python image with nothing installed.
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

C_KM_S = 299792.458


class Latest:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.body = ""


def serve(latest: Latest, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] != "/api/detection":
                self.send_error(404)
                return
            with latest.lock:
                body = latest.body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


class Forwarder:
    """blah2-api's tracker socket: reconnects, and drops a frame it cannot send."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.sock: socket.socket | None = None

    def send(self, line: str) -> None:
        try:
            if self.sock is None:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            self.sock.sendall((line + "\n").encode())
        except OSError:
            self.sock = None


def synthetic(fc_hz: float, max_delay_km: float, doppler_max_hz: float, period_s: float):
    wavelength_km = C_KM_S / fc_hz
    rng = random.Random(7)
    # (first frame, last frame, starting delay km, doppler Hz)
    targets = [
        (0, 10**9, 0.25 * max_delay_km, -0.3 * doppler_max_hz),
        (10, 70, 0.55 * max_delay_km, 0.25 * doppler_max_hz),
        (90, 10**9, 0.40 * max_delay_km, 0.15 * doppler_max_hz),
    ]
    k = 0
    while True:
        dets = []
        for first, last, d0, dop in targets:
            if first <= k <= last:
                delay = d0 + (-wavelength_km * dop) * period_s * (k - first)
                dets.append(
                    (round(delay + rng.gauss(0, 0.05), 2), round(dop + rng.gauss(0, 0.5), 2), 18.0)
                )
        for _ in range(rng.randint(0, 3)):
            dets.append(
                (
                    round(rng.uniform(0.05, 0.95) * max_delay_km, 2),
                    round(rng.uniform(-0.9, 0.9) * doppler_max_hz, 2),
                    round(rng.uniform(5, 9), 1),
                )
            )
        rng.shuffle(dets)
        yield {
            "timestamp": int(time.time() * 1000),
            "delay": [d[0] for d in dets],
            "doppler": [d[1] for d in dets],
            "snr": [d[2] for d in dets],
        }
        k += 1
        time.sleep(period_s)


def relay(url: str):
    last = None
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                body = response.read().decode()
        except OSError:
            body = ""
        if body.strip():
            frame = json.loads(body)
            if frame.get("timestamp") != last:
                last = frame.get("timestamp")
                yield body
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("synthetic", "relay"), default="synthetic")
    parser.add_argument("--serve-port", type=int, default=13000)
    parser.add_argument("--tracker-port", type=int, default=39100)
    parser.add_argument("--fc-hz", type=float, default=204.64e6)
    parser.add_argument("--max-delay-km", type=float, default=60.0)
    parser.add_argument("--doppler-max-hz", type=float, default=200.0)
    parser.add_argument("--period-s", type=float, default=0.9)
    args = parser.parse_args()

    latest = Latest()
    threading.Thread(target=serve, args=(latest, args.serve_port), daemon=True).start()
    forwarder = Forwarder(args.tracker_port)

    if args.source == "relay":
        frames = relay("http://127.0.0.1:3000/api/detection")
    else:
        frames = (
            json.dumps(f)
            for f in synthetic(args.fc_hz, args.max_delay_km, args.doppler_max_hz, args.period_s)
        )
    for n, line in enumerate(frames, start=1):
        # Stored, then forwarded, in that order, as blah2-api does.
        with latest.lock:
            latest.body = line
        forwarder.send(line)
        if n % 20 == 0:
            print(f"frame source: {n} frames", flush=True)


if __name__ == "__main__":
    main()
