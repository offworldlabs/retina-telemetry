#!/usr/bin/env bash
#
# Drive the server's refusals against a real node.
#
#   tools/live-failures.sh [ssh-host]        # default: owl
#
# tools/live-service.sh proves the happy path. This proves the paths that
# actually matter, because every one of them is a rule the node has to obey
# rather than a request it has to make:
#
#   409  →  resend the configuration and carry on, without replaying the frame
#   429  →  honour Retry-After rather than our own schedule
#   pause→  stop posting detections, keep heartbeating
#   resume  start again without being told twice
#   401  →  stop the stream, keep beating, and NEVER re-register
#
# The unit tests cover all five in-process. What this adds is real timing on
# real hardware with real detections flowing, where a wrong sleep or a missed
# level shows up as a gap in the request log rather than an assertion.
#
# Evidence is the mock's own record: a timeline of what arrived and when. After
# the 401 you should see detections stop dead while heartbeats continue.

set -euo pipefail

HOST="${1:-owl}"
PORT="${MOCK_PORT:-18080}"
IMAGE="${PROBE_IMAGE:-python:3.11-slim}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON" ]] || { echo "no venv at $PYTHON — run: pip install -e '.[dev]'" >&2; exit 1; }
MOCK_PID=""
SSH_PID=""

cleanup() {
  [[ -n "$SSH_PID" ]] && kill "$SSH_PID" 2>/dev/null || true
  [[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null || true
}
trap cleanup EXIT

control() { curl -s -X POST "http://127.0.0.1:$PORT/_control/$1" -d "$2" >/dev/null; }
mark() { printf '\033[1m  t+%-3s %s\033[0m\n' "$1" "$2"; }

echo "→ starting mock ingest on 127.0.0.1:$PORT"
"$PYTHON" "$REPO_ROOT/tools/mock_server.py" --port "$PORT" --quiet &
MOCK_PID=$!
sleep 2

echo "→ shipping package to $HOST"
REMOTE_DIR="/tmp/retina-fail.$$"
tar czf - -C "$REPO_ROOT" retina_telemetry \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR/app' && tar xzf - -C '$REMOTE_DIR/app'"

echo "→ starting the service on $HOST"
ssh -o ExitOnForwardFailure=yes -R "$PORT:127.0.0.1:$PORT" "$HOST" \
  "REMOTE_DIR='$REMOTE_DIR' PORT='$PORT' IMAGE='$IMAGE' bash -s" >/tmp/live-failures.log 2>&1 <<'REMOTE' &
set -euo pipefail
SCRATCH="$REMOTE_DIR/scratch"
mkdir -p "$SCRATCH"
python3 - "$SCRATCH" <<'PY'
import json, sys, pathlib, yaml
scratch = pathlib.Path(sys.argv[1])
config = yaml.safe_load(pathlib.Path("/data/retina-node/config/config.yml").read_text())
config["location"]["rx"]["beam_width"] = 60
(scratch / "config.yml").write_text(yaml.safe_dump(config))
(scratch / "consent.json").write_text(json.dumps({
    "opted_in": True,
    "agreement": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
}))
PY
docker run --rm --network host --pull missing \
  -e PYTHONUNBUFFERED=1 -e LOG_LEVEL=INFO \
  -e RETINA_API_URL="http://127.0.0.1:${PORT}/v1" \
  -e BLAH2_API_URL="http://127.0.0.1:3000" \
  -e NODE_ID_PATH=/data/mender/node_id \
  -e DEVICE_TYPE_PATH=/data/mender/device_type \
  -e CONFIG_PATH=/scratch/config.yml -e CONSENT_PATH=/scratch/consent.json \
  -e TOKEN_PATH=/scratch/token -e STATUS_PATH=/scratch/status.json \
  -e DISK_PATH=/data/mender \
  -e HEARTBEAT_INTERVAL_S=6 -e STATUS_INTERVAL_S=3 \
  -v "$REMOTE_DIR/app:/app:ro" -v "$SCRATCH:/scratch" \
  -v /data/mender:/data/mender:ro -w /app \
  "$IMAGE" sh -c "pip install --quiet --no-cache-dir requests PyYAML pydantic \
                  && timeout 80 python -m retina_telemetry; true"
echo "── status document at the end ──"
cat "$SCRATCH/status.json"
rm -rf "$REMOTE_DIR"
REMOTE
SSH_PID=$!

echo "→ waiting for the node to register and start streaming"
for _ in $(seq 60); do
  sleep 1
  [[ $(curl -s "http://127.0.0.1:$PORT/_control/state" | grep -c '"token_issued": true') -gt 0 ]] && break
done
echo

echo "── injecting failures ──────────────────────────────────────"
sleep 8
mark 0 "409 on the next detection — expect a config resend, frame abandoned"
control enqueue '{"endpoint":"detection","status":409}'

sleep 8
mark 8 "429 with Retry-After: 5 — expect the delay honoured"
control enqueue '{"endpoint":"detection","status":429,"retry_after":5}'

sleep 10
mark 18 "streaming_allowed=false — expect detections to stop, beats to continue"
control levels '{"streaming_allowed": false}'

sleep 10
mark 28 "streaming_allowed=true — expect the stream to resume unprompted"
control levels '{"streaming_allowed": true}'

sleep 10
mark 38 "401 on every detection — expect the stream to stop and NO re-registration"
control enqueue '{"endpoint":"detection","status":401,"count":50}'

sleep 14
echo
echo "→ collecting"
wait "$SSH_PID" 2>/dev/null || true
tail -n 20 /tmp/live-failures.log

echo
echo "── the timeline the mock recorded ──────────────────────────"
curl -s "http://127.0.0.1:$PORT/_control/requests" | "$PYTHON" "$REPO_ROOT/tools/summarise_run.py" --timeline
