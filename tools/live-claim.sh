#!/usr/bin/env bash
#
# Drive the claim endpoints on a real node, against the real server.
#
#   tools/live-claim.sh <ssh-host> read
#   tools/live-claim.sh <ssh-host> offer --email X --confirm-address X --confirm-send
#   tools/live-claim.sh <ssh-host> watch [--seconds 600]
#   tools/live-claim.sh <ssh-host> offer-again --email X
#   tools/live-claim.sh <ssh-host> resend --confirm-send
#
# ## This one is not like the others
#
# Every other live-*.sh points the node at tools/mock_server.py over an SSH
# reverse tunnel. This one does not. It talks to whichever server minted the
# node's token, which for a node with no RETINA_API_URL override is production.
# `offer` makes that server send a real person an email, and a click on the
# link in it binds this node to the account behind the address. Releasing it is
# the owner's to do from the dashboard and cannot be undone from the node.
#
# So the phases are separate invocations rather than one script that runs
# start to finish. `read` and `watch` send no mail and are safe to repeat.
#
# ## Why it does not run the service
#
# It would be wrong twice over. The service registers on startup, and the spec
# answers a node that already holds a valid token with the opaque 403, so it
# would never get a token; the one path where registration does succeed revokes
# the token the node's live container is using, which the server alerts on. And
# a second process with the same node_id posting frames would interleave `seq`
# and `boot_id` with the real container's and make the node look like it was
# flapping. So this drives the claim endpoints only, with the token the node
# already holds, and the live container keeps streaming throughout.
#
# ## Safety
#
# /data is mounted read-only, so the token cannot be written even by accident,
# and nothing is copied off the device. The only writable path is a scratch
# directory holding a copy of the package, removed on exit. The retina-node
# compose project is not touched and nothing is restarted.

set -euo pipefail

HOST="${1:?usage: tools/live-claim.sh <ssh-host> <phase> [args...]}"
shift
PHASE="${1:?usage: tools/live-claim.sh <ssh-host> <phase> [args...]}"
shift

IMAGE="${PROBE_IMAGE:-python:3.11-slim}"
API_URL="${RETINA_API_URL:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "→ host:  $HOST"
echo "→ phase: $PHASE $*"
if [ -n "$API_URL" ]; then
  echo "→ api:   $API_URL (overridden)"
else
  echo "→ api:   the built-in default, which is production"
fi
echo

REMOTE_DIR="/tmp/retina-claim.$$"
tar czf - -C "$REPO_ROOT" retina_telemetry tools/probe_claim.py \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR/app' && tar xzf - -C '$REMOTE_DIR/app'"

ssh "$HOST" "REMOTE_DIR='$REMOTE_DIR' IMAGE='$IMAGE' API_URL='$API_URL' bash -s" -- "$PHASE" "$@" <<'REMOTE'
set -euo pipefail
PHASE="$1"; shift

cleanup() { rm -rf "$REMOTE_DIR"; }
trap cleanup EXIT

# /data read-only: the token is read and must never be written. --network host
# for parity with how the node's own telemetry container reaches the server.
docker run --rm --network host \
  --pull missing \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONUNBUFFERED=1 \
  -v "$REMOTE_DIR/app:/app:ro" \
  -v /data:/data:ro \
  -w /app \
  "$IMAGE" \
  sh -c "pip install --quiet --no-cache-dir --timeout 60 --retries 10 requests PyYAML pydantic \
         && python -m tools.probe_claim ${API_URL:+--api-url '$API_URL'} $PHASE $*"
REMOTE
