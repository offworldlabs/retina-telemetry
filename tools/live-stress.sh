#!/usr/bin/env bash
#
# Stress the service on a node across process restarts and a broken config.
#
#   tools/live-stress.sh [ssh-host] [seconds-per-phase]   # default: owl, 60
#
# `live-service.sh` runs one process once and answers "does the happy path
# work". This answers the questions that only appear across a *restart* and a
# *failure*, both of which spec v1.1.1 changed the behaviour of:
#
#   1. baseline      one process, streaming normally
#   2. restart       a second process sharing the first's token
#   3. broken config the same token, with an unreadable config.yml
#
# What each phase is actually for:
#
#   boot_id (Q10)   restart-local `seq` is only interpretable alongside it.
#                   Phase 2 must show a *different* boot_id and a `seq` that
#                   starts over, and the server must be able to tell that from
#                   dropped frames. Nothing before v1.1.1 could express this.
#
#   no re-register  a restarted node reuses its persisted token. A second
#                   registration here would mean we had turned every restart
#                   into a registration, which the rate limits punish.
#
#   Q16             phase 3 is the case the spec changed the shape for: a node
#                   that cannot build a NodeConfig can never PUT one, so it can
#                   never be issued a config_version. It must still heartbeat,
#                   carrying `config_version: null` and saying what is wrong in
#                   errors[]. Before v1.1.1 it went silent instead — the node
#                   most worth hearing from being the one that disappeared.
#
# The token lives in a scratch directory that survives between phases and is
# deleted at the end, so this never touches the node's real /data token.
#
# ## Safety
#
# Identical envelope to live-service.sh: every node path mounted read-only, no
# docker socket, no published ports, --rm. **Nothing stops or restarts blah2**,
# so the radar keeps running throughout and the node is unaffected. The only
# process restarted is our own container.

set -euo pipefail

HOST="${1:-owl}"
PHASE_SECONDS="${2:-60}"
PORT="${MOCK_PORT:-18080}"
IMAGE="${PROBE_IMAGE:-python:3.11-slim}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$( [ -x "$REPO_ROOT/.venv/bin/python" ] && echo "$REPO_ROOT/.venv/bin/python" || command -v python3 )}"

cleanup() { [ -n "${MOCK_PID:-}" ] && kill "$MOCK_PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "→ starting the mock on 127.0.0.1:$PORT"
"$PYTHON" "$REPO_ROOT/tools/mock_server.py" --port "$PORT" >/dev/null 2>&1 &
MOCK_PID=$!
sleep 1
curl -sf "http://127.0.0.1:$PORT/_control/state" >/dev/null || { echo "mock did not start" >&2; exit 1; }

echo "→ shipping package to $HOST"
REMOTE_DIR="/tmp/retina-stress.$$"
tar czf - -C "$REPO_ROOT" retina_telemetry \
  | ssh "$HOST" "mkdir -p '$REMOTE_DIR/app' && tar xzf - -C '$REMOTE_DIR/app'"

echo "→ three phases of ${PHASE_SECONDS}s against the tunnelled mock"
echo

ssh -o ExitOnForwardFailure=yes -R "$PORT:127.0.0.1:$PORT" "$HOST" \
  "REMOTE_DIR='$REMOTE_DIR' PORT='$PORT' IMAGE='$IMAGE' RUN_FOR='$PHASE_SECONDS' bash -s" <<'REMOTE'
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
    "licence": ACCEPTED,
    "remote_management": ACCEPTED,
    "publication": {**ACCEPTED, "choice": "public"},
}))
PY

run_phase () {
  docker run --rm --network host --pull missing \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONUNBUFFERED=1 -e LOG_LEVEL=INFO \
    -e RETINA_API_URL="http://127.0.0.1:${PORT}/v1" \
    -e BLAH2_API_URL="http://127.0.0.1:3000" \
    -e NODE_ID_PATH=/data/mender/node_id \
    -e DEVICE_TYPE_PATH=/data/mender/device_type \
    -e CONFIG_PATH=/scratch/config.yml \
    -e CONSENT_PATH=/scratch/consent.json \
    -e TOKEN_PATH=/scratch/token \
    -e STATUS_PATH=/scratch/status.json \
    -e DISK_PATH=/data/mender \
    -e HEARTBEAT_INTERVAL_S=10 -e STATUS_INTERVAL_S=5 \
    -v "$REMOTE_DIR/app:/app:ro" -v "$SCRATCH:/scratch" -v /data/mender:/data/mender:ro \
    -w /app "$IMAGE" \
    sh -c "pip install --quiet --no-cache-dir requests PyYAML pydantic \
           && timeout ${RUN_FOR} python -m retina_telemetry; true" 2>&1 | tail -3
}

echo "=== phase 1: baseline ==="
run_phase
echo "    token persisted: $([ -s "$SCRATCH/token" ] && echo yes || echo NO)"

echo
echo "=== phase 2: restart, same token ==="
run_phase
echo "    state: $(python3 -c "import json;d=json.load(open('$SCRATCH/status.json'));print(d['state'], 'seq='+str(d['seq']))")"

echo
echo "=== phase 3: same token, unreadable config ==="
cp "$SCRATCH/config.yml" "$SCRATCH/config.good.yml"
printf 'this is not: [valid yaml\n' > "$SCRATCH/config.yml"
run_phase
echo "    status: $(python3 -c "
import json; d=json.load(open('$SCRATCH/status.json'))
print(d['state'], '|', (d.get('detail') or '')[:90])")"
mv "$SCRATCH/config.good.yml" "$SCRATCH/config.yml"

rm -rf "$REMOTE_DIR"
REMOTE

echo
echo "── what the mock received, across all three phases ─────────"
curl -s "http://127.0.0.1:$PORT/_control/requests" > /tmp/stress-requests.json
"$PYTHON" "$REPO_ROOT/tools/summarise_run.py" < /tmp/stress-requests.json

echo
echo "── the questions this run exists to answer ─────────────────"
"$PYTHON" - /tmp/stress-requests.json <<'PY'
import json, sys
reqs = json.load(open(sys.argv[1]))["requests"]
det = [r["body"] for r in reqs if r["endpoint"] == "detection"]
beats = [r["body"] for r in reqs if r["endpoint"] == "heartbeat"]
regs = [r for r in reqs if r["endpoint"] == "register"]

boots = list(dict.fromkeys(b["boot_id"] for b in det + beats))
print(f"  boot_id values seen         {len(boots)}   {boots}")
print(f"  registrations               {len(regs)}   (1 = the restart reused its token)")

for b in boots:
    seqs = [d["seq"] for d in det if d["boot_id"] == b]
    if seqs:
        lost = max(seqs) - min(seqs) + 1 - len(seqs)
        print(f"    boot {b[:8]}…  seq {min(seqs)} → {max(seqs)}, {len(seqs)} frames, {lost} dropped")

null_cv = [b for b in beats if b["config_version"] is None]
print(f"  beats with config_version null   {len(null_cv)} of {len(beats)}   (Q16)")
if null_cv:
    errs = next((b["errors"] for b in null_cv if b["errors"]), [])
    print(f"    and errors[] carried             {errs[:1]}")
print(f"  states reported             {sorted({b['state'] for b in beats})}")

dropped = [k for k in ("config_version",) if any(k not in b for b in beats)]
dropped += [f"health.{k}" for k in ("cpu_pct", "disk_free_mb", "temp_c", "blah2")
            if any(k not in b.get("health", {}) for b in beats)]
print(f"  required-nullable keys dropped   {dropped or 'none'}")
PY
