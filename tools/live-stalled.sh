#!/usr/bin/env bash
#
# Reach NodeState.stalled on a real node, by stopping its radar.
#
#   tools/live-stalled.sh [ssh-host]        # default: owl
#
# **This stops blah2 on a live radar node.** Every other script in tools/ is
# read-only by design; this one is not, and it is the only way to reach the one
# state spec v1.1.1 added. Run it deliberately.
#
# `stalled` is "the client is healthy and blah2 has stopped producing after
# having produced". The server raises it against the radar rather than the node,
# which is the distinction `error` could not make — before v1.1.1 a working node
# with a dead radar paged whoever owns the client.
#
# The sequence:
#
#   1. service starts, streams normally           -> streaming
#   2. blah2 containers stopped                   -> the poll starts failing
#   3. once nothing has arrived for a while       -> stalled, and blah2: "down"
#   4. blah2 restarted, frames resume             -> streaming
#
# ## Restoring the node
#
# `restore` runs from an EXIT trap, so blah2 comes back whether this succeeds,
# fails, or is interrupted. It runs over its own ssh connection rather than
# reusing the tunnelled one, so a dead tunnel cannot strand the radar. The last
# thing printed is the verified state of blah2-api, and a non-zero exit means
# the radar did NOT come back and needs a look.
#
# Only the four blah2 containers are touched. retina-gui, tar1090, adsb2dd and
# the rest of the stack are left alone, and nothing on disk is modified.

set -euo pipefail

HOST="${1:-owl}"
PORT="${MOCK_PORT:-18080}"
IMAGE="${PROBE_IMAGE:-python:3.11-slim}"
#: Container names, not compose service names — the service is `blah2_api`, the
#: container is `blah2-api`. Stopped by name with plain `docker stop` rather
#: than through compose: it needs no compose file, no sudo (the ssh user is in
#: the docker group), and it touches exactly these four and nothing else.
#: `docker stop` also overrides `restart: always` until an explicit start, which
#: is what makes the stall hold long enough to observe.
BLAH2="blah2 blah2-api blah2-web blah2-host"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$( [ -x "$REPO_ROOT/.venv/bin/python" ] && echo "$REPO_ROOT/.venv/bin/python" || command -v python3 )}"

restore() {
    echo
    echo "→ restoring blah2 on $HOST"
    ssh -o ConnectTimeout=15 "$HOST" "docker start $BLAH2" >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        if ssh -o ConnectTimeout=10 "$HOST" \
             'curl -sf -m 3 -o /dev/null http://127.0.0.1:3000/api/detection' 2>/dev/null; then
            echo "   blah2-api answering again — node restored"
            return 0
        fi
        sleep 3
    done
    echo "   *** blah2-api NOT answering. The radar is still down; check $HOST. ***" >&2
    return 1
}

cleanup() { [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null || true; }
trap 'cleanup; restore' EXIT

echo "→ starting the mock on 127.0.0.1:$PORT   (watch it at http://127.0.0.1:$PORT/)"
"$PYTHON" "$REPO_ROOT/tools/mock_server.py" --port "$PORT" >/dev/null 2>&1 &
MOCK_PID=$!
sleep 1
curl -sf "http://127.0.0.1:$PORT/_control/state" >/dev/null || { echo "mock did not start" >&2; exit 1; }

echo "→ shipping package to $HOST"
REMOTE_DIR="/tmp/retina-stalled.$$"
tar czf - -C "$REPO_ROOT" retina_telemetry \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR/app' && tar xzf - -C '$REMOTE_DIR/app'"

echo "→ running: 40s streaming, blah2 stopped, 70s stalled, blah2 restarted"
echo

ssh -o ExitOnForwardFailure=yes -R "$PORT:127.0.0.1:$PORT" "$HOST" \
  "REMOTE_DIR='$REMOTE_DIR' PORT='$PORT' IMAGE='$IMAGE' BLAH2='$BLAH2' bash -s" <<'REMOTE'
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

# The radar comes back even if this shell dies mid-run.
trap "docker start $BLAH2 >/dev/null 2>&1 || true" EXIT

docker run -d --name retina-stalled --network host --pull missing \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 -e LOG_LEVEL=INFO \
  -e RETINA_API_URL="http://127.0.0.1:${PORT}/v1" \
  -e BLAH2_API_URL="http://127.0.0.1:3000" \
  -e NODE_ID_PATH=/data/mender/node_id -e DEVICE_TYPE_PATH=/data/mender/device_type \
  -e CONFIG_PATH=/scratch/config.yml -e CONSENT_PATH=/scratch/consent.json \
  -e TOKEN_PATH=/scratch/token -e STATUS_PATH=/scratch/status.json \
  -e DISK_PATH=/data/mender -e HEARTBEAT_INTERVAL_S=10 -e STATUS_INTERVAL_S=5 \
  -v "$REMOTE_DIR/app:/app:ro" -v "$SCRATCH:/scratch" -v /data/mender:/data/mender:ro \
  -w /app "$IMAGE" \
  sh -c "pip install --quiet --no-cache-dir requests PyYAML pydantic \
         && python -m retina_telemetry" >/dev/null

say_state () { python3 -c "
import json
try:
    d = json.load(open('$SCRATCH/status.json'))
    print('   state:', d['state'], '| seq', d['seq'])
except Exception as e:
    print('   status not written yet')"; }

echo "=== 1. streaming normally (40s) ==="
sleep 40; say_state

echo
echo "=== 2. stopping blah2 ==="
docker stop $BLAH2 >/dev/null 2>&1
echo "   stopped: $BLAH2"

echo
echo "=== 3. waiting for the node to notice (70s) ==="
sleep 70; say_state

echo
echo "=== 4. restarting blah2 ==="
docker start $BLAH2 >/dev/null 2>&1
sleep 35; say_state

docker rm -f retina-stalled >/dev/null 2>&1 || true
rm -rf "$REMOTE_DIR"
REMOTE

echo
echo "── what the mock saw ───────────────────────────────────────"
curl -s "http://127.0.0.1:$PORT/_control/requests" > /tmp/stalled-requests.json
"$PYTHON" - /tmp/stalled-requests.json <<'PY'
import json, sys
reqs = json.load(open(sys.argv[1]))["requests"]
beats = [(r["at"][11:19], r["body"]) for r in reqs if r["endpoint"] == "heartbeat"]
det = [r["at"][11:19] for r in reqs if r["endpoint"] == "detection"]
print(f"  detections {len(det)}   first {det[0] if det else '-'}   last {det[-1] if det else '-'}")
print("\n  every heartbeat, in order:")
for at, b in beats:
    h = b.get("health") or {}
    print(f"    {at}  state={b['state']:<10} blah2={str(h.get('blah2')):<6} "
          f"adsb={str(h.get('adsb')):<5} errors={len(b.get('errors') or [])}")
states = [b["state"] for _, b in beats]
print(f"\n  states in order: {' → '.join(dict.fromkeys(states))}")
print(f"  reached stalled: {'YES' if 'stalled' in states else 'NO'}")
PY
