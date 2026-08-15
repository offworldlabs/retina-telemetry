#!/usr/bin/env bash
#
# Drive the edges a normal run never reaches, on a real node.
#
#   tools/live-edges.sh [ssh-host] [seconds-per-phase]   # default: owl, 45
#
# The other live scripts exercise the paths a node takes. This exercises the
# ones it takes *once*, and which have therefore broken twice already without
# anyone noticing until a review.
#
# blah2 is not touched. Every edge is produced by feeding the service a
# purpose-built blah2-api stand-in on a spare port — so the real radar keeps
# running, and the node is unaffected throughout.
#
#   1. flood        5000 detections in one frame. Truncated to the spec's 512,
#                   and the body must stay inside the 64 KiB origin cap.
#   2. non-finite   inf and nan in delay/doppler/snr. These serialise as the
#                   bare tokens Infinity and NaN, which are not valid JSON, so
#                   the whole body would be refused by a strict parser. Reachable
#                   for real: snr is 10*log10(|x|) - noisePower, and a
#                   zero-magnitude detection gives -inf.
#   3. warmup       an empty 200, which blah2-api serves until the first CPI.
#                   Must read as "up, nothing yet", not as unreachable.
#   4. garbage      a 200 whose body is not JSON at all.
#   5. errors flood twenty distinct 600-character faults. The count bound and
#                   the per-message bound each hold, and compose to 10 KiB
#                   against an 8 KiB heartbeat cap the origin applies *before*
#                   parsing — so the beat that matters most is the one dropped.
#
# The mock validates every request against the generated models, so a payload
# that would fail at the server fails here. Watch it at http://127.0.0.1:18080/.

set -euo pipefail

HOST="${1:-owl}"
PHASE_SECONDS="${2:-45}"
PORT="${MOCK_PORT:-18080}"
FAKE_PORT="${FAKE_BLAH2_PORT:-13099}"
IMAGE="${PROBE_IMAGE:-python:3.11-slim}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$( [ -x "$REPO_ROOT/.venv/bin/python" ] && echo "$REPO_ROOT/.venv/bin/python" || command -v python3 )}"

cleanup() { [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "→ starting the mock on 127.0.0.1:$PORT   (watch it at http://127.0.0.1:$PORT/)"
"$PYTHON" "$REPO_ROOT/tools/mock_server.py" --port "$PORT" >/dev/null 2>&1 &
MOCK_PID=$!
sleep 1
curl -sf "http://127.0.0.1:$PORT/_control/state" >/dev/null || { echo "mock did not start" >&2; exit 1; }

echo "→ shipping package to $HOST"
REMOTE_DIR="/tmp/retina-edges.$$"
tar czf - -C "$REPO_ROOT" retina_telemetry \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR/app' && tar xzf - -C '$REMOTE_DIR/app'"

echo "→ five phases of ${PHASE_SECONDS}s, against a synthetic blah2 on :$FAKE_PORT"
echo

ssh -o ExitOnForwardFailure=yes -R "$PORT:127.0.0.1:$PORT" "$HOST" \
  "REMOTE_DIR='$REMOTE_DIR' PORT='$PORT' FAKE_PORT='$FAKE_PORT' IMAGE='$IMAGE' RUN_FOR='$PHASE_SECONDS' bash -s" <<'REMOTE'
set -euo pipefail
SCRATCH="$REMOTE_DIR/scratch"
mkdir -p "$SCRATCH"

python3 - "$SCRATCH" <<'PY'
import json, sys, pathlib, yaml
scratch = pathlib.Path(sys.argv[1])
config = yaml.safe_load(pathlib.Path("/data/retina-node/config/config.yml").read_text())
(scratch / "config.yml").write_text(yaml.safe_dump(config))
ACCEPTED = {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"}
(scratch / "consent.json").write_text(json.dumps({
    "licence": ACCEPTED, "remote_management": ACCEPTED,
    "publication": {**ACCEPTED, "choice": "public"},
}))
PY

# A blah2-api stand-in serving whichever edge `mode` names. Real blah2 keeps
# running on :3000 untouched; the service under test is pointed here instead.
cat > "$SCRATCH/fake_blah2.py" <<'PY'
import json, math, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("EDGE_MODE", "flood")
PORT = int(os.environ.get("FAKE_PORT", "13099"))


def body():
    now = int(time.time() * 1000)
    if MODE == "warmup":
        return ""                                     # blah2-api before the first CPI
    if MODE == "garbage":
        return "<html>not json at all</html>"
    if MODE == "flood":
        n = 5000
        return json.dumps({"timestamp": now,
                           "delay": [12.4 + i % 97 for i in range(n)],
                           "doppler": [-118.0 + i % 89 for i in range(n)],
                           "snr": [5.0 + i % 11 for i in range(n)]})
    if MODE == "nonfinite":
        return json.dumps({"timestamp": now,
                           "delay": [12.4, math.inf, 30.1, 40.0],
                           "doppler": [-118.0, 1.0, math.nan, 2.0],
                           "snr": [14.2, 2.0, 3.0, -math.inf]},
                          allow_nan=True)
    return json.dumps({"timestamp": now, "delay": [], "doppler": [], "snr": []})


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = body().encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PY

run_phase () {
  local mode="$1" blah2_url="$2"
  EDGE_MODE="$mode" FAKE_PORT="$FAKE_PORT" python3 "$SCRATCH/fake_blah2.py" &
  local fake=$!
  sleep 1
  docker run --rm --network host --pull missing \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 -e LOG_LEVEL=INFO \
    -e RETINA_API_URL="http://127.0.0.1:${PORT}/v1" \
    -e BLAH2_API_URL="$blah2_url" \
    -e NODE_ID_PATH=/data/mender/node_id -e DEVICE_TYPE_PATH=/data/mender/device_type \
    -e CONFIG_PATH=/scratch/config.yml -e CONSENT_PATH=/scratch/consent.json \
    -e TOKEN_PATH=/scratch/token -e STATUS_PATH=/scratch/status.json \
    -e DISK_PATH=/data/mender -e HEARTBEAT_INTERVAL_S=10 -e STATUS_INTERVAL_S=5 \
    -v "$REMOTE_DIR/app:/app:ro" -v "$SCRATCH:/scratch" -v /data/mender:/data/mender:ro \
    -w /app "$IMAGE" \
    sh -c "pip install --quiet --no-cache-dir --timeout 60 --retries 10 requests PyYAML pydantic \
           && timeout ${RUN_FOR} python -m retina_telemetry; true" 2>&1 | tail -2
  kill $fake 2>/dev/null || true
  python3 -c "
import json
d = json.load(open('$SCRATCH/status.json'))
print('    state:', d['state'], '| detail:', (d.get('detail') or '-')[:70])
print('    errors:', len(d['errors']), 'distinct')"
}

FAKE="http://127.0.0.1:$FAKE_PORT"
for phase in "flood:$FAKE" "nonfinite:$FAKE" "warmup:$FAKE" "garbage:$FAKE"; do
  mode="${phase%%:*}"; url="${phase#*:}"
  echo "=== $mode ==="
  run_phase "$mode" "$url"
  echo
done

echo "=== errors flood (twenty distinct long faults) ==="
# Produced by pointing the service at a port nothing is listening on, with a
# very long URL so each connection error is a distinct 600-character message.
LONGHOST="http://127.0.0.1:1/$(python3 -c 'print("x"*600)')"
run_phase "empty" "$LONGHOST"

docker rm -f retina-edges >/dev/null 2>&1 || true
rm -rf "$REMOTE_DIR"
REMOTE

echo
echo "── what the mock accepted ──────────────────────────────────"
curl -s "http://127.0.0.1:$PORT/_control/requests" > /tmp/edges-requests.json
"$PYTHON" - /tmp/edges-requests.json <<'PY'
import json, sys
reqs = json.load(open(sys.argv[1]))["requests"]
det = [r["body"] for r in reqs if r["endpoint"] == "detection"]
beats = [r["body"] for r in reqs if r["endpoint"] == "heartbeat"]

if det:
    sizes = [len(d["delay"]) for d in det]
    print(f"  detection frames accepted   {len(det)}")
    print(f"    detections per frame      min {min(sizes)}  max {max(sizes)}  (spec maxItems 512)")
    bodies = [len(json.dumps(d).encode()) for d in det]
    print(f"    largest body              {max(bodies)} bytes  (origin cap 65536)")
    parallel = all(len({len(d[k]) for k in ('delay','doppler','snr','adsb_hex')}) == 1 for d in det)
    print(f"    all four arrays parallel  {parallel}")
    nonfinite = [d for d in det
                 if any(v != v or v in (float('inf'), float('-inf'))
                        for k in ('delay','doppler','snr') for v in d[k])]
    print(f"    frames carrying inf/nan   {len(nonfinite)}  (must be 0)")

if beats:
    sizes = [len(json.dumps(b).encode()) for b in beats]
    worst = max(range(len(beats)), key=lambda i: sizes[i])
    print(f"\n  heartbeats accepted         {len(beats)}")
    print(f"    largest body              {max(sizes)} bytes  (origin cap 8192)")
    print(f"    errors in that beat       {len(beats[worst].get('errors') or [])}")
    print(f"    states seen               {sorted({b['state'] for b in beats})}")
PY
