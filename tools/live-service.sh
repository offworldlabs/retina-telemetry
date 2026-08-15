#!/usr/bin/env bash
#
# Run the whole service on a node, against a mock ingest API hosted here.
#
#   tools/live-service.sh [ssh-host] [seconds]      # default: owl, 45
#
# The end-to-end test the three probes were building toward: real identity,
# real configuration, real detections from blah2, real HTTP — with the server
# replaced by tools/mock_server.py running on this machine.
#
# ## How the node reaches a mock on a laptop
#
# An SSH reverse tunnel. The mock binds 127.0.0.1 and should stay that way, so
# `ssh -R` publishes it on the node's own loopback instead of exposing a port.
# Nothing is reachable from any network and the tunnel dies with the session.
# Not port 8080 — tar1090 already holds that on the node.
#
# ## The one synthetic value, and why
#
# Everything is real except the records that do not exist on any node yet:
#
#   consent records  three of them, and the retina-gui wizard persists none
#
# The antenna geometry is deliberately NOT synthesised. Since spec v1.1.1 both
# beam fields are nullable, so a node without them sends explicit nulls — that
# is what every node in the fleet does, and the run should exercise it rather
# than paper over it.
#
# The consent file is written to a scratch directory and pointed at with an env
# var. The configuration is copied verbatim from the node's own config.yml, so
# every coordinate, frequency and bin count is the real one.
#
# ## Safety
#
# Read-only mounts for everything belonging to the node. The scratch directory
# is the only writable path and is deleted afterwards. Nothing touches the
# retina-node compose project and nothing is restarted. The only traffic to the
# real internet is a pip install.

set -euo pipefail

HOST="${1:-owl}"
SECONDS_TO_RUN="${2:-45}"
PORT="${MOCK_PORT:-18080}"
IMAGE="${PROBE_IMAGE:-python:3.11-slim}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
[[ -x "$PYTHON" ]] || { echo "no venv at $PYTHON — run: pip install -e '.[dev]'" >&2; exit 1; }
MOCK_PID=""

cleanup() {
  [[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo "→ starting mock ingest on 127.0.0.1:$PORT"
"$PYTHON" "$REPO_ROOT/tools/mock_server.py" --port "$PORT" --quiet &
MOCK_PID=$!
sleep 2

curl -sf "http://127.0.0.1:$PORT/_control/state" >/dev/null || {
  echo "mock did not start" >&2
  exit 1
}

echo "→ shipping package to $HOST"
REMOTE_DIR="/tmp/retina-live.$$"
tar czf - -C "$REPO_ROOT" retina_telemetry \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR/app' && tar xzf - -C '$REMOTE_DIR/app'"

echo "→ running the service on $HOST for ${SECONDS_TO_RUN}s, against the tunnelled mock"
echo

ssh -o ExitOnForwardFailure=yes -R "$PORT:127.0.0.1:$PORT" "$HOST" \
  "REMOTE_DIR='$REMOTE_DIR' PORT='$PORT' IMAGE='$IMAGE' RUN_FOR='$SECONDS_TO_RUN' bash -s" <<'REMOTE'
set -euo pipefail
SCRATCH="$REMOTE_DIR/scratch"
mkdir -p "$SCRATCH"

# The node's own configuration, verbatim. Nothing substituted.
python3 - "$SCRATCH" <<'PY'
import json, sys, pathlib, yaml
scratch = pathlib.Path(sys.argv[1])
config = yaml.safe_load(pathlib.Path("/data/retina-node/config/config.yml").read_text())
(scratch / "config.yml").write_text(yaml.safe_dump(config))
# Synthetic. Three separately versioned records since the 2026-08-10
# revision; `publication` is a privacy decision the wizard must actually put
# to the owner, and nothing on a node may manufacture one.
ACCEPTED = {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"}
(scratch / "consent.json").write_text(json.dumps({
    "licence": ACCEPTED,
    "remote_management": ACCEPTED,
    "publication": {**ACCEPTED, "choice": "public"},
}))
print(f"   rx {config['location']['rx']['latitude']}, {config['location']['rx']['longitude']}"
      f" @ {config['location']['rx']['altitude']} m")
print(f"   tx {config['location']['tx']['name']!r} @ {config['location']['tx']['altitude']} m")
print(f"   fc {config['capture']['fc']}  fs {config['capture']['fs']}"
      f"  delayMax {config['process']['ambiguity']['delayMax']}")
PY
echo

docker run --rm --network host \
  --pull missing \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONUNBUFFERED=1 \
  -e LOG_LEVEL=INFO \
  -e RETINA_API_URL="http://127.0.0.1:${PORT}/v1" \
  -e BLAH2_API_URL="http://127.0.0.1:3000" \
  -e NODE_ID_PATH=/data/mender/node_id \
  -e DEVICE_TYPE_PATH=/data/mender/device_type \
  -e CONFIG_PATH=/scratch/config.yml \
  -e CONSENT_PATH=/scratch/consent.json \
  -e TOKEN_PATH=/scratch/token \
  -e STATUS_PATH=/scratch/status.json \
  -e DISK_PATH=/data/mender \
  -e HEARTBEAT_INTERVAL_S=10 \
  -e STATUS_INTERVAL_S=5 \
  -v "$REMOTE_DIR/app:/app:ro" \
  -v "$SCRATCH:/scratch" \
  -v /data/mender:/data/mender:ro \
  -w /app \
  "$IMAGE" \
  sh -c "pip install --quiet --no-cache-dir --timeout 60 --retries 10 requests PyYAML pydantic \
         && timeout ${RUN_FOR} python -m retina_telemetry; true"

echo
echo "── the status document the node wrote ──────────────────────"
cat "$SCRATCH/status.json"
rm -rf "$REMOTE_DIR"
REMOTE

echo
echo "── what the mock received ──────────────────────────────────"
curl -s "http://127.0.0.1:$PORT/_control/requests" \
  | "$PYTHON" "$REPO_ROOT/tools/summarise_run.py"
