"""Vulture dead-code whitelist.

Read by tools/check-dead-code.sh. Every name here is one vulture reports as
dead but which must not be deleted — or which nobody has decided about yet.
The distinction matters, so the two live in separate sections.

Add to CONTRACTS only when the name is genuinely referenced by something
vulture cannot see: a framework calling in, a wire format, a config key. Real
dead code should be deleted, not whitelisted.

The UNREVIEWED section is a backlog, not an exemption. Each entry is code that
appears genuinely unreachable and needs a decision — delete it, or wire up
whatever was left unfinished. The gate is green with these listed so that it
starts catching NEW dead code immediately; working through them is separate.
"""
# ruff: noqa: B018, F821
# B018 — bare-name expressions are how vulture whitelists work.
# F821 — these names are defined in other modules; only vulture reads this file.

_ = type("_", (), {})()

# ── Contracts: referenced by something vulture cannot see ─────────────────────
# generated from docs/node-ingest-v1.yml; part of the wire contract
#   retina_telemetry/wire/models.py:241
ConfigResponse
# generated from docs/node-ingest-v1.yml; part of the wire contract
#   retina_telemetry/wire/models.py:147
DetectionAck
# generated from docs/node-ingest-v1.yml; part of the wire contract
#   retina_telemetry/wire/models.py:252
Error
# generated from docs/node-ingest-v1.yml; part of the wire contract
#   retina_telemetry/wire/models.py:212
HeartbeatResponse
# generated from docs/node-ingest-v1.yml; part of the wire contract
#   retina_telemetry/wire/models.py:70
RegisterResponse
# read by socketserver.ThreadingMixIn
#   tools/mock_server.py:392
_.daemon_threads
# http.server.BaseHTTPRequestHandler dispatches by method name
#   tools/mock_server.py:193
_.do_GET
# http.server.BaseHTTPRequestHandler dispatches by method name
#   tools/mock_server.py:233
_.do_POST
# http.server.BaseHTTPRequestHandler dispatches by method name
#   tools/mock_server.py:230
_.do_PUT
# read by BaseHTTPRequestHandler to set the HTTP version
#   tools/mock_server.py:142
protocol_version

# ── UNREVIEWED: appears dead, needs a decision (delete, or finish wiring) ──────
# TODO: no reference found anywhere in the estate
#   tools/mock_server.py:370  (unused method)
_._config
# TODO: no reference found anywhere in the estate
#   tools/mock_server.py:344  (unused method)
_._detection
# TODO: no reference found anywhere in the estate
#   tools/mock_server.py:358  (unused method)
_._heartbeat
# TODO: no reference found anywhere in the estate
#   tools/mock_server.py:331  (unused method)
_._register
# TODO: no reference found anywhere in the estate
#   tools/probe_report.py:24  (unused function)
failures
