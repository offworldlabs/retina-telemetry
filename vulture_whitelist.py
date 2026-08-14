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

# retina_telemetry/wire/models.py is GENERATED from docs/node-ingest-v1.yml by
# tools/generate-models.sh. Vulture can never usefully analyse it, for two
# reasons that will not change:
#
#   - pydantic reads `model_config` and RootModel's `root` itself, so nothing in
#     our code names them.
#   - the enum members are the spec's vocabulary. A value we deliberately never
#     send — `stopping`, `unknown` — is still part of the contract, and the ones
#     we do send travel as strings, so the attribute is never named either.
#
# Listed without line numbers on purpose. They churn on every regeneration, and
# the stale ones this replaces pointed at unrelated code after the 2026-08-10
# spec revision. If a future revision adds a name, this gate fails and someone
# reads it — which is the right outcome, since a new name in the wire contract
# is worth a glance.
_.model_config
_.root

# NodeState.stopping — never sent; a final beat during shutdown means a network
# call on the way out. See Q17.
stopping
# Blah2.unknown / Adsb.unknown — we distinguish "have not looked" (omit the
# field) from "looked and could not tell", and only the first happens to us.
unknown
# PublicationChoice.public / .private — carried as strings, so the members are
# never named.
public
private
# Blah2.NoneType_None — v1.1.1 added `null` to the enum alongside a nullable
# type, and the generator turned that into a real member. It is redundant (the
# type union already makes the field nullable) and nothing should ever use it.
# Flagged to the server author as worth deleting from the enum list.
NoneType_None
# NodeConfig.delay_tolerance_us — set in wire/config.py, but only ever as a
# keyword argument, because the source attribute is `delay_tolerance_km`. The
# rename across the unit conversion is the point of the naming convention, and
# the cost is that vulture cannot see the wire-side name being used.
delay_tolerance_us

# The generated schema classes. Nothing constructs the response types: the
# client validates against them by name, which vulture does not follow.
ConfigResponse
DetectionAck
HeartbeatResponse
RegisterResponse
# Two of these exist because datamodel-codegen collided on the name. `Error` is
# now the item type of HeartbeatRequest.errors, and `Error1` is the actual error
# response schema from components/schemas/Error. Both are generated; neither is
# ours to rename.
Error
Error1

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

# _Handler dispatches endpoint handlers by name at mock_server.py:438 —
#   getattr(self, f"_{endpoint}")(body)
# so vulture sees four definitions and no callers. Reviewed 2026-08-14: all four
# are reachable, and the UNREVIEWED backlog that used to list them said "no
# reference found anywhere in the estate", which was simply untrue. The fifth
# entry, probe_report.failures(), *was* dead and is deleted rather than
# whitelisted.
_._config
_._detection
_._heartbeat
_._register
