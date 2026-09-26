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
# coordinate, frequency and bin count is the real one. The contact document is
# the node's own too, and absent on a node whose owner skipped that step, which
# is the ordinary case and sends nothing.
#
# ## UNSITE=1
#
# Nulls the geometry in the scratch copy only, to exercise the unsited path the
# spec opened across v1.2.0 and v1.2.2: a node whose owner has not picked a
# tower registers with seven explicit nulls rather than holding. The node's own
# config.yml is never written to, here or anywhere in this script.
#
# ## TRACKS=synthetic | TRACKS=relay
#
# Tries a retina-tracker build beside the node's own, to exercise the tracks a
# frame carries since node ingest 1.6.0. TRACKER_SRC names a retina-tracker
# checkout (default: the sibling repo, on whatever branch it has out). Two
# throwaway containers join the run, both on loopback ports nothing else uses:
#
#   - a tracker: the node's own retina-tracker image with TRACKER_SRC's package
#     mounted over it, control on 39101, ingest on 39100
#   - tools/frame_source.py on 13000, standing in for blah2-api's detection
#     path: it stores each frame, serves it, and forwards the same bytes to
#     that tracker. `synthetic` makes frames consistent with the node's own
#     centre frequency and span; `relay` passes on blah2-api's real ones.
#
# The service is pointed at the pair instead of blah2-api and the node's
# tracker. The real tracker, and the real blah2-api, are never written to.
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
#: CONTACT=1 writes an obviously-fake contact document into the scratch
#: directory to exercise PUT /nodes/contact. Off by default, in which case the
#: node's own file is used and a node whose owner skipped that step correctly
#: sends nothing at all. The node's /data is never written to either way.
CONTACT="${CONTACT:-0}"
#: Where the service asks for tracks. The node's own retina-tracker by default;
#: point it elsewhere to try a tracker build beside the running one.
TRACKER_URL_GIVEN="${TRACKER_URL:-}"
TRACKER_URL="${TRACKER_URL:-http://127.0.0.1:30101}"
TRACKS="${TRACKS:-}"
TRACKER_SRC="${TRACKER_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../retina-tracker" 2>/dev/null && pwd)}"
BLAH2_URL="http://127.0.0.1:3000"
if [[ -n "$TRACKS" ]]; then
  [[ "$TRACKS" == synthetic || "$TRACKS" == relay ]] || { echo "TRACKS is synthetic or relay" >&2; exit 1; }
  [[ -d "$TRACKER_SRC/retina_tracker" ]] || { echo "no retina-tracker checkout at $TRACKER_SRC" >&2; exit 1; }
  # Unless one was named: TRACKER_URL=http://127.0.0.1:30101 feeds the node's
  # own tracker nothing and asks it anyway, which is what every node does until
  # a tracker with /frame is released.
  TRACKER_URL="${TRACKER_URL_GIVEN:-http://127.0.0.1:39101}"
  BLAH2_URL="http://127.0.0.1:13000"
fi
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
tar czf - -C "$REPO_ROOT" retina_telemetry tools/frame_source.py \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR/app' && tar xzf - -C '$REMOTE_DIR/app'"
if [[ -n "$TRACKS" ]]; then
  echo "→ shipping retina-tracker from $TRACKER_SRC ($(git -C "$TRACKER_SRC" rev-parse --abbrev-ref HEAD) $(git -C "$TRACKER_SRC" rev-parse --short HEAD))"
  tar czf - -C "$TRACKER_SRC" --exclude=__pycache__ retina_tracker \
    | ssh "$HOST" "mkdir -p '$REMOTE_DIR/tracker' && tar xzf - -C '$REMOTE_DIR/tracker'"
fi

echo "→ running the service on $HOST for ${SECONDS_TO_RUN}s, against the tunnelled mock"
echo

ssh -o ExitOnForwardFailure=yes -R "$PORT:127.0.0.1:$PORT" "$HOST" \
  "REMOTE_DIR='$REMOTE_DIR' PORT='$PORT' IMAGE='$IMAGE' RUN_FOR='$SECONDS_TO_RUN' UNSITE='$UNSITE' CONTACT='$CONTACT' TRACKER_URL='$TRACKER_URL' TRACKS='$TRACKS' BLAH2_URL='$BLAH2_URL' bash -s" <<'REMOTE'
set -euo pipefail
SCRATCH="$REMOTE_DIR/scratch"
mkdir -p "$SCRATCH"

# The node's own document by default. Absent on a node whose owner skipped the
# step, which is the ordinary case and must send nothing.
# Container paths, not host paths: the scratch directory is mounted at /scratch,
# the same way the consent file is handed over below.
CONTACT_FILE=/data/retina-gui/telemetry-contact.json
if [ "${CONTACT:-0}" = "1" ]; then
  cat > "$SCRATCH/contact.json" <<'JSON'
{"first_name": "Test", "last_name": "Owner",
 "email": "not-a-real-address@example.com", "phone": "+441234567890",
 "country": "GB"}
JSON
  CONTACT_FILE=/scratch/contact.json
  echo "   CONTACT=1: a fake contact document in the scratch directory"
fi

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

# The tag of the tracker image actually running, so versions.retina_tracker
# says what compose would pass as RETINA_TRACKER_V.
TRACKER_IMAGE="$(docker inspect retina-tracker --format '{{.Config.Image}}' 2>/dev/null || true)"
TRACKER_V="${TRACKER_IMAGE##*:}"

if [ -n "$TRACKS" ]; then
  [ -n "$TRACKER_IMAGE" ] || { echo "no retina-tracker image on this node to borrow" >&2; exit 1; }
  case "$TRACKER_URL" in *:39101*) TRACKER_V="$TRACKER_V+branch" ;; esac
  side_down() { docker rm -f live-tracker live-frames >/dev/null 2>&1 || true; }
  trap side_down EXIT
  side_down
  GEOMETRY="$(python3 -c '
import yaml
c = yaml.safe_load(open("/data/retina-node/config/config.yml"))
amb = c["process"]["ambiguity"]
print(c["capture"]["fc"], amb["delayMax"] * 299792.458 / c["capture"]["fs"], amb["dopplerMax"])
')"
  read -r FC MAXKM DOPMAX <<< "$GEOMETRY"
  docker run -d --rm --name live-tracker --network host \
    -v "$REMOTE_DIR/tracker/retina_tracker:/app/retina_tracker:ro" \
    -v /data/retina-node/config:/config:ro \
    "$TRACKER_IMAGE" \
    python -m retina_tracker.track_detections --tcp --tcp-host 127.0.0.1 --tcp-port 39100 \
      --control-host 127.0.0.1 --control-port 39101 -s /tmp/events.jsonl \
      -c /config/retina-tracker.yaml --blah2-config /config/config.yml >/dev/null
  docker run -d --rm --name live-frames --network host \
    -v "$REMOTE_DIR/app/tools:/tools:ro" python:3.11-slim \
    python -u /tools/frame_source.py --source "$TRACKS" --serve-port 13000 --tracker-port 39100 \
      --fc-hz "$FC" --max-delay-km "$MAXKM" --doppler-max-hz "$DOPMAX" >/dev/null
  echo "   TRACKS=$TRACKS: side tracker on 39100/39101, frames on 13000 (fc $FC, ${MAXKM%.*} km, +-$DOPMAX Hz)"
  sleep 3
fi
echo "   tracker: ${TRACKER_V:-none running}, asked at $TRACKER_URL"

# CONFIG_POLL_S: the default 30 s is longer than a short run, and that loop is
# also what notices a changed contact document, so it has to tick at least once.
docker run --rm --network host \
  --pull missing \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONUNBUFFERED=1 \
  -e LOG_LEVEL=INFO \
  -e RETINA_API_URL="http://127.0.0.1:${PORT}/v1" \
  -e BLAH2_API_URL="$BLAH2_URL" \
  -e TRACKER_URL="$TRACKER_URL" \
  -e RETINA_TRACKER_V="$TRACKER_V" \
  -e NODE_ID_PATH=/data/mender/node_id \
  -e DEVICE_TYPE_PATH=/data/mender/device_type \
  -e CONFIG_PATH=/scratch/config.yml \
  -e CONSENT_PATH=/scratch/consent.json \
  -e CONTACT_PATH="$CONTACT_FILE" \
  -e TOKEN_PATH=/scratch/token \
  -e STATUS_PATH=/scratch/status.json \
  -e DISK_PATH=/data/mender \
  -e WIZARD_FLAG_PATH=/data/retina-gui/setup-wizard-completed \
  -e HEARTBEAT_INTERVAL_S=10 \
  -e CONFIG_POLL_S=5 \
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
if [ -n "$TRACKS" ]; then
  echo
  echo "── the side tracker, as it finished ────────────────────────"
  curl -s -m3 http://127.0.0.1:39101/health; echo
  docker logs live-frames 2>&1 | tail -2
fi
rm -rf "$REMOTE_DIR"
REMOTE

echo
echo "── what the mock received ──────────────────────────────────"
REQUESTS_JSON="$(curl -s "http://127.0.0.1:$PORT/_control/requests")"
#: REQUESTS_OUT=path keeps every request the mock received, bodies included,
#: for checks the summary does not make.
[[ -n "${REQUESTS_OUT:-}" ]] && printf '%s' "$REQUESTS_JSON" > "$REQUESTS_OUT"
printf '%s' "$REQUESTS_JSON" | "$PYTHON" "$REPO_ROOT/tools/summarise_run.py"
