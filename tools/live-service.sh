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
# ## What is real, and what is not
#
# Everything is real. The consent records are read from the node's own
# /data/retina-gui/telemetry-consent.json, which retina-gui v0.7.0 now writes;
# three synthetic ones are used only if that file is missing, and the run says
# which it used. The wizard flag is the node's own too, so the gate this service
# holds registration on is exercised rather than bypassed.
#
# The antenna geometry is deliberately NOT synthesised. Both beam fields are
# nullable, so a node without them sends explicit nulls, which is what every
# node in the fleet does.
#
# The configuration is copied verbatim from the node's own config.yml, so every
# coordinate, frequency and bin count is the real one.
#
# ## UNSITE=1
#
# Nulls the geometry in the scratch copy only, to exercise the unsited path the
# spec opened across v1.2.0 and v1.2.2: a node whose owner has not picked a
# tower registers with seven explicit nulls rather than holding. The node's own
# config.yml is never written to, here or anywhere in this script.
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
UNSITE="${UNSITE:-0}"
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
  "REMOTE_DIR='$REMOTE_DIR' PORT='$PORT' IMAGE='$IMAGE' RUN_FOR='$SECONDS_TO_RUN' UNSITE='$UNSITE' bash -s" <<'REMOTE'
set -euo pipefail
SCRATCH="$REMOTE_DIR/scratch"
mkdir -p "$SCRATCH"

# The node's own configuration, verbatim. Nothing substituted.
python3 - "$SCRATCH" "$UNSITE" <<'PY'
import json, sys, pathlib, shutil, yaml
scratch, unsite = pathlib.Path(sys.argv[1]), sys.argv[2] == "1"
config = yaml.safe_load(pathlib.Path("/data/retina-node/config/config.yml").read_text())

if unsite:
    # The scratch copy only. This is what retina-node's default.yml ships, and
    # what a node looks like before its owner reaches the tower step.
    for end in ("rx", "tx"):
        config["location"][end] = dict.fromkeys(
            ("latitude", "longitude", "altitude", "name"), None
        )
    print("   UNSITE=1: geometry nulled in the scratch copy, node config untouched")

(scratch / "config.yml").write_text(yaml.safe_dump(config))

# The node's own records, which retina-gui v0.7.0 writes. Synthesised only when
# absent, because `publication` is a privacy decision the wizard must actually
# put to the owner and nothing on a node may manufacture one.
real = pathlib.Path("/data/retina-gui/telemetry-consent.json")
if real.is_file():
    shutil.copyfile(real, scratch / "consent.json")
    print(f"   consent: the node's own, {len(json.loads(real.read_text()))} records")
else:
    ACCEPTED = {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"}
    (scratch / "consent.json").write_text(json.dumps({
        "licence": ACCEPTED,
        "remote_management": ACCEPTED,
        "publication": {**ACCEPTED, "choice": "public"},
    }))
    print("   consent: SYNTHETIC, the node has none")

location = config["location"]
print(f"   rx {location['rx']['latitude']}, {location['rx']['longitude']}"
      f" @ {location['rx']['altitude']} m")
print(f"   tx {location['tx']['name']!r} at {location['tx']['latitude']}, "
      f"{location['tx']['longitude']} @ {location['tx']['altitude']} m")
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
  -e WIZARD_FLAG_PATH=/data/retina-gui/setup-wizard-completed \
  -e HEARTBEAT_INTERVAL_S=10 \
  -e STATUS_INTERVAL_S=5 \
  -v "$REMOTE_DIR/app:/app:ro" \
  -v "$SCRATCH:/scratch" \
  -v /data/mender:/data/mender:ro \
  -v /data/retina-gui:/data/retina-gui:ro \
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
