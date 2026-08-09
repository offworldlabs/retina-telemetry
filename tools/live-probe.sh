#!/usr/bin/env bash
#
# Run the stage 1 collection modules against a real node, inside a container.
#
#   tools/live-probe.sh [ssh-host] [stage]   # default: owl, both stages
#
#   tools/live-probe.sh owl 1    # collection only
#   tools/live-probe.sh owl 2    # wire only — real node data through the builders
#
# Ships the package to a temp directory over ssh, runs the probes in a stock
# python image with the node's paths mounted read-only, and removes the temp
# directory afterwards. Nothing is built, nothing is pushed to a registry, and
# nothing persists on the node.
#
# Stage 1 tests the environment: /proc not being namespaced, statvfs being
# namespaced, the mounts. Stage 2 is a pure transform and behaves identically
# anywhere, so what it tests is that *real* values survive the conversions —
# and it prints the exact JSON the server would receive, which no unit test can,
# because our fixtures are ours and these numbers are not.
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
STAGE="${2:-all}"
IMAGE="${PROBE_IMAGE:-python:3.11-slim}"

case "$STAGE" in
  1)   PROBES="tools/probe_collection.py" ;;
  2)   PROBES="tools/probe_wire.py" ;;
  all) PROBES="tools/probe_collection.py tools/probe_wire.py" ;;
  *)   echo "stage must be 1, 2 or all — got '$STAGE'" >&2; exit 2 ;;
esac
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
tar czf - -C "$REPO_ROOT" \
  retina_telemetry \
  tools/probe_report.py tools/probe_collection.py tools/probe_wire.py \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR' && tar xzf - -C '$REMOTE_DIR'"

echo "→ running stage $STAGE in $IMAGE (host network, all mounts read-only)"
echo
# Each probe exits with its own failure count; `set -e` inside the container
# would stop at the first, so they are chained explicitly and the codes summed.
RUN="pip install --quiet --no-cache-dir requests PyYAML pydantic; rc=0"
for probe in $PROBES; do
  RUN="$RUN; python /app/$probe || rc=\$((rc + \$?))"
done
RUN="$RUN; exit \$rc"

# --network host so 127.0.0.1:3000 reaches blah2-api, matching the real service.
# PYTHONDONTWRITEBYTECODE so a :ro source mount cannot cause __pycache__ noise.
ssh -t "$HOST" "docker run --rm \
  --network host \
  --pull missing \
  -e PYTHONDONTWRITEBYTECODE=1 \
  ${MOUNTS[*]} \
  '$IMAGE' \
  sh -c '$RUN'"
