#!/usr/bin/env bash
#
# Run the stage 1 collection modules against a real node, inside a container.
#
#   tools/live-probe.sh [ssh-host]        # default: owl
#
# Ships the package to a temp directory over ssh, runs tools/probe_collection.py
# in a stock python image with the node's paths mounted read-only, and removes
# the temp directory afterwards. Nothing is built, nothing is pushed to a
# registry, and nothing persists on the node.
#
# Safety envelope, because this targets a live radar node:
#   - every mount is :ro, and the set mirrors the planned stage 4 mounts
#   - no docker socket, no published ports, --rm
#   - the only traffic is a handful of GETs to /api/detection, which retina-gui
#     polls anyway
#   - the container is torn down whether the probe passes or fails
#
# It does NOT touch the retina-node compose project. Nothing is restarted.

set -euo pipefail

HOST="${1:-owl}"
IMAGE="${PROBE_IMAGE:-python:3.11-slim}"
REMOTE_DIR="/tmp/retina-telemetry-probe.$$"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The mount set the real service is planned to have. Kept here rather than in
# the probe so that running this validates the deployment plan, not just the code.
MOUNTS=(
  -v "$REMOTE_DIR:/app:ro"
  -v /data/mender:/data/mender:ro
  -v /data/retina-node/config:/data/retina-node/config:ro
  -v /data/retina-gui:/data/retina-gui:ro
)

cleanup() {
  ssh "$HOST" "rm -rf '$REMOTE_DIR'" 2>/dev/null || true
}
trap cleanup EXIT

echo "→ target: $HOST"
ssh -o ConnectTimeout=10 "$HOST" true || {
  echo "cannot reach $HOST over ssh" >&2
  exit 1
}

echo "→ shipping package to $REMOTE_DIR"
tar czf - -C "$REPO_ROOT" retina_telemetry tools/probe_collection.py \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR' && tar xzf - -C '$REMOTE_DIR'"

echo "→ running probe in $IMAGE (host network, all mounts read-only)"
echo
# --network host so 127.0.0.1:3000 reaches blah2-api, matching the real service.
# PYTHONDONTWRITEBYTECODE so a :ro source mount cannot cause __pycache__ noise.
ssh -t "$HOST" "docker run --rm \
  --network host \
  --pull missing \
  -e PYTHONDONTWRITEBYTECODE=1 \
  ${MOUNTS[*]} \
  '$IMAGE' \
  sh -c 'pip install --quiet --no-cache-dir requests PyYAML && python /app/tools/probe_collection.py'"
