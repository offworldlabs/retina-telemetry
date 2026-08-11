# Implementation plan

## What this service is

One container per node that owns everything the node sends to `api.retina.fm`.
Registration, detection streaming, heartbeat, config sync. Nothing else on the node
talks to the server.

The reasoning for unifying it: detections, config and health all need the same
retry, auth, backoff and clock-offset machinery, and duplicating that across three
services is how you end up with three different retry bugs.

## The one architectural rule

**It must not share fate with the stack it reports on.**

The most valuable telemetry moment is when blah2 is crash-looping — exactly when a
container that depends on blah2 being healthy would also be dead. Therefore:

- No `depends_on` gated on blah2 or retina-tracker health.
- Every local source is optional. All of them absent is a valid running state that
  still heartbeats.
- "blah2 has been down for 6 minutes" is a payload, not an absence of payloads.

## Scope boundary

Uplink only. Mender owns OTA; retina-gui owns config authority. This service does not
push anything down and does not accept inbound connections.

That said, the spec already has a downlink in all but name — `config_stale` and
`streaming_allowed` are server-controlled levels restated on every response. If a real
command channel is ever needed, a `commands[]` array in the same response bodies gets
it for free without a new protocol. Do not build it now.

---

## Transport model

The spec is unambiguous and it simplifies things considerably:

**Latest wins, at most one request in flight, no queue.** While a POST is slow, a newer
frame *replaces* the pending one rather than queueing behind it. A request past its
timeout (a few seconds) is abandoned so the next frame goes out fresh.

Consequences:

- **No spool for detections.** Gaps are permanent and that is intended — a frame that
  arrives after retina-analytics' 4 s association window closes is worse than one that
  never arrives, because it associates wrongly rather than being ignored.
- Config and registration are the opposite: must not lose, retry until they land.
- One kept-alive TLS connection, shared across all three endpoints.

So there are exactly two traffic disciplines, not the five-class scheme a general
telemetry agent would need:

| Class | Discipline |
|---|---|
| Detections | latest-wins, drop freely, one in flight |
| Registration / config / heartbeat | must land, retry with jittered exponential backoff |

---

## The three layers

The build is staged by **layer**, not by endpoint. An earlier draft phased this
registration → heartbeat → detections, which meant the retry, auth and response-level
machinery — shared by all four endpoints — had no single home and would have been
written up to four times.

| Stage | Owns | Knows about |
|---|---|---|
| **1 — Collection** | the interfaces to the rest of the node stack | the node only |
| **2 — Construction** | turning what we collected into wire payloads | both sides |
| **3 — Communication** | sending them, and everything that guards that | the server only |

The constraint that keeps the layers honest:

> **Stage 3 knows nothing about radar. Stage 1 knows nothing about the server.**
> Stage 2 is the only place that knows both.

If a bistatic delay appears in `comms/`, or an OpenAPI schema type appears in
`collect/`, the boundary has leaked. This is worth enforcing in review — it is what
makes stages 1 and 2 fully testable with no server and no mock.

### Stage boundaries

**1 → 2: source-faithful records, units in the field names.**
Stage 1 hands over exactly what the node produced — kilometres, epoch milliseconds,
metres — under names that say so: `delay_km`, `timestamp_ms`, `rx_alt_m`. Stage 2's
output uses the spec's names and the spec's units: `delay`, `t`, `rx_alt_ft`. A
conversion that goes missing is then visible at the call site rather than on the wire.

This is also why **no unit conversion happens in stage 1**. `docs/data-sources.md` is a
document about source units; if stage 1 converted, that document would stop describing
stage 1's output and the two would drift apart.

**2 → 3: wire-ready payload objects matching the spec schemas.**
Stage 3 serialises and sends. It does not build, derive, or convert.

---

## Process shape

Single Python process, threads (matches retina-gui's house style; there are only three
loops and one shared slot, so asyncio buys nothing here).

```
main  ── lifecycle supervisor (stage 3b): owns the state machine, gates the rest
 ├─ detection thread   poll blah2-api ~4 Hz → dedupe on timestamp → publish to slot
 ├─ sender thread      take slot → convert → POST → apply ack levels
 ├─ heartbeat thread   60 s → collect health → POST → apply levels
 ├─ config watcher     hash config.yml → PUT on change / on config_stale / on 409
 └─ status writer      mirror current state to disk for retina-gui
```

The "slot" is a single-element mailbox: writing replaces whatever is there. That *is*
latest-wins, expressed in one data structure.

**The threads are not peers.** Heartbeat needs a token and a `config_version`;
detections need both plus `streaming_allowed`. None of those exist until registration
succeeds, and registration can legitimately sit in `403` for hours waiting on an
operator to reactivate the node. So the supervisor gates thread startup rather than
letting three loops spin against a `None` token. Only the status writer runs
unconditionally, because a node that cannot register is precisely the node whose
operator needs to be told why.

### Module layout

```
retina-telemetry/
├── retina_telemetry/
│   ├── __main__.py          wiring, signal handling, thread lifecycle
│   ├── state.py             token (0600), seq/boot_id, node_ref, config_version
│   │                        — the concurrency boundary; shared state is touched only here
│   ├── status.py            the document retina-gui reads
│   ├── errors.py            bounded accumulator for the heartbeat errors[] field
│   │
│   ├── collect/             ── stage 1 ──
│   │   ├── blah2.py         poll, dedupe, structural validation, derived liveness
│   │   ├── node_config.py   config.yml read + change hashing (no conversion)
│   │   ├── identity.py      /data/mender/node_id reader — raises, never defaults
│   │   ├── consent.py       licence, remote management and publication records
│   │   └── host.py          cpu / temp / disk / uptime from /proc, /sys, statvfs
│   │
│   ├── wire/                ── stage 2 ──
│   │   ├── models.py        GENERATED from the spec — tools/generate-models.sh
│   │   ├── units.py         km→µs, ms→s, m→ft, max_range_km derivation
│   │   ├── detection.py     DetectionPoll → DetectionFrame: units, adsb_hex, seq
│   │   ├── config.py        NodeConfigRaw → NodeConfig
│   │   ├── heartbeat.py     health + state + versions + errors → HeartbeatRequest
│   │   └── registration.py  → RegisterRequest (one field blocked: Q2)
│   │
│   └── comms/               ── stage 3 ──
│       ├── client.py   3a   session, auth, backoff, Retry-After, clock offset
│       ├── levels.py   3a   shared response-level applier
│       ├── lifecycle.py 3b  state machine, registration gating, opt-out
│       ├── reliable.py 3c   must-land: register, config, heartbeat
│       └── stream.py   3c   latest-wins: detections
│
├── tests/
│   ├── collect/  wire/  comms/    mirroring the package
│   └── fakes/               scripted server, captured blah2-api responses
├── tools/
│   ├── generate-models.sh   regenerate wire/models.py; --check guards drift
│   ├── mock_server.py       local stand-in for api.retina.fm; hosted or in-process
│   ├── live-probe.sh        run the probes on a node over ssh
│   ├── probe_collection.py  stage 1 — tests the container environment
│   ├── probe_wire.py        stage 2 — real data through the builders
│   └── probe_report.py      shared reporting, so exit codes aggregate
├── docs/
├── deploy/                  compose entry — see the packaging note in stage 4
├── pyproject.toml
└── Dockerfile
```

Three naming decisions worth recording, because each replaces something an earlier
draft had:

- **`collect/` not `ingress/`** — it matches the stage name in this document verbatim,
  so the tree needs no translation to the plan. `collect/ wire/ comms/` also reads as
  the pipeline itself.
- **`wire/` not `build/`** — `build/` is in `.gitignore`, so that package would have
  been silently untracked. `wire/` is the better name regardless: the folder exists to
  be the boundary where node units become spec units.
- **`status.py` at the top level, and no `egress/`** — it reads `state.py` and writes a
  file, reflecting all three stages rather than collecting anything, which makes it a
  peer of `state.py`. A folder for one module is overhead.

The package sits at the repo root rather than under `src/`. `src/` layout exists to stop
an installed package being shadowed by the working directory, which matters for
published libraries; this is copied into an image and run. Root-level means pytest needs
no editable install. This diverges from retina-gui, which puts loose modules directly in
`src/` with no package at all — deliberately, since that does not scale to three layers.

### Response handling is shared, not per-endpoint

Every response carries the same levels, restated in full. `comms/levels.py` applies
them wherever they appear — this is the module the old per-endpoint layout had nowhere
to put:

| Signal | Action |
|---|---|
| `config_stale: true` | trigger `PUT /nodes/config` |
| `streaming_allowed: false` | stop *posting* detections; keep polling, keep beating |
| `node_ref` differs | overwrite the display cache; nothing functional depends on it |
| `server_time` | compute clock offset, log/report if large |
| `401` | stop streaming, surface locally, keep heartbeating, **never re-register** |
| `409` on detection | PUT config, then resume |
| `429` | honour `Retry-After`, drop the skipped frames |

The `401` rule is the one worth a test: treating revocation as a trigger to re-register
turns a deliberate revocation into a registration storm.

---

## Stages

### Stage 0 — scaffolding

- Repo skeleton, `pyproject.toml`, Dockerfile, ruff/pytest.
- Payload validation derived from `docs/node-ingest-v1.yml`, so stage 2 can be checked
  against the real contract without a server running.
- **`tools/mock_server.py`** — a local stand-in for `api.retina.fm`. Needed from 3a
  onward; stages 1 and 2 need no server at all, not even a mock.

### Stage 1 — collection

The four incoming interfaces and the one outgoing one. Full inventory in
`docs/data-sources.md` §0.

- `blah2.py` — poll `/api/detection` and dedupe on `timestamp`. Reports whether the poll
  reached blah2-api; stage 2 turns that into `NodeHealth.blah2`. No time-dependent
  behaviour and no spec vocabulary. **Only that endpoint** — `/api/timing` and the
  capture status endpoints have no field in the spec (§5 of `data-sources.md`).

  **The equal-length assertion lives here, not in `wire/`.** It is a claim about whether
  the source is internally coherent, it needs no knowledge of the server, and checking at
  the read point means a malformed frame never enters the slot at all. Mapping
  associations down to `.hex` and synthesising nulls stay in `wire/`, because those are
  facts about the spec rather than about blah2.
- `node_config.py` — read the read-only mount. Change detection is frozen-dataclass
  equality: mapping the document already discards everything we do not send, so a
  reformat or an unmapped edit cannot trigger a `PUT /nodes/config`.
- `identity.py` — the hard-error behaviour, and a test that `'Unknown'` can never
  appear in a payload.
- `consent.py` — the three acceptance records, read from
  `/data/retina-gui/telemetry-consent.json`. A missing file means nothing was accepted,
  which is a normal state — it is the state of every node today — so this module is
  complete and testable before retina-gui writes it. Nothing here is ever synthesised:
  a missing record means the owner was not shown that text.
- `host.py` — cpu, temp, disk, uptime, and nothing else. `/proc` is not namespaced, so
  `cpu_pct` is correctly host-wide; `statvfs` *is*, so it must be called on `/data`
  rather than `/` or it measures the container's overlay.

`status.py` is not stage 1 — it reflects state from all three — but it is the other half
of the contract with retina-gui and is worth writing early, since it is the only way the
stage 3b failures ever reach an operator.

**Deliverable:** a process that can report everything it can see about the node and
nothing about the server. No network calls off-box.

**No docker socket.** blah2 and ADS-B liveness fall out of the detection poll, and
`versions` come from compose env vars. Dropping the socket removes a mount from the one
container on the node that talks to the internet.

### Stage 2 — construction and packaging

- `models.py` — **generated** from `docs/node-ingest-v1.yml`, never hand-edited.
  `tools/generate-models.sh` regenerates; `--check` fails if the checked-in copy is
  stale, which is the CI guard.
- `units.py` with property tests: km→µs, ms→s, m→ft, `max_range_km` derivation. These
  are the highest-value tests in the repo — three conversions where a silent error is
  plausible and invisible on the wire.
- `detection.py` — convert km→µs and ms→s, synthesise `adsb_hex` nulls when ADS-B is off
  (`DetectionPoll.adsb is None`), map association objects down to `.hex`, attach `seq`
  and `config_version`. Arrays arrive already checked for equal length by `collect/`.
- `config.py`, `heartbeat.py`, `registration.py`.

**Deliverable:** given stage 1's output, valid wire payloads. Still no network.

**Traceability is the organising principle**, because a passthrough layer that hides its
mappings is worthless. Builders take stage 1's own types in their signatures rather than
dictionaries, so anything they need that stage 1 cannot collect is a keyword-only
argument whose docstring names the module that will supply it; every builder carries a
field map in its docstring; and `wire/__init__.py` holds the complete provenance for all
four payloads, including the rows marked "caller" that mark where `state.py` and stage 3
plug in.

**Blocked, and scoped around:** the registration payload needs the three acceptance
records (Q2). The antenna geometry stopped blocking on 2026-08-11, when the spec made
both beam fields optional — `build_node_config` now succeeds on a real node and has
executed against live data. `build_registration` still raises `IncompletePayload` until
retina-gui persists consent, which is tested.

#### Serialisation, and the module that used to be here

Payloads go out as `model_dump(mode="json", exclude_none=True)`. Both arguments carry
weight:

- **`mode="json"`** encodes the acceptance timestamps. Without it they stay as `datetime`
  objects and `json.dumps` refuses the registration payload outright — it would fail at
  send time with nothing having validated it.
- **`exclude_none=True`** drops what we do not know, which is the honest report for an
  unreadable `cpu_pct` or an uncharacterised antenna.

`wire/serialise.py` used to sit here enforcing a stricter rule: *drop a `None` only if
the field is optional; keep it if the spec requires it, even when its value is null.*
That mattered because `NodeConfig.beam_azimuth_deg` was **required and nullable** —
`null` was how the spec spelled broadside/omnidirectional, so dropping the key produced a
payload the server rejects, and `NodeConfig` is nested inside `RegisterRequest`, which
made it propagate to the one request a node cannot recover from failing. Found by
`tools/probe_wire.py` printing a real payload, not by any unit test.

**The 2026-08-11 revision made both beam fields optional, leaving the spec with no
required-nullable field at all.** With no subject, `to_wire` and `exclude_none=True`
became the same function, and the module was deleted rather than carried inert.

The trade is that a future revision adding such a field would break payloads silently.
`tests/wire/test_payload_encoding.py` turns that into a loud failure — one canary
asserting no payload field is both required and nullable, another asserting every payload
survives `json.dumps`. Both were verified to fire against the exact regression they
guard. Restoring the old behaviour means reinstating `wire/serialise.py` from git
history.

### Stage 3a — the machinery

Session, auth, jittered exponential backoff, `Retry-After`, clock offset against
`server_time`, and the shared level applier. Built once, used by all four endpoints.

Testable in full against `tools/mock_server.py` before any endpoint logic exists.

#### The mock is scriptable, not generated

Generating one from the OpenAPI document (Prism) gives correctly-shaped replies for
free, but it always cooperates — and everything worth testing here is the server
*refusing*. A revoked token must stop the stream **without** re-registering, since
treating a `401` as a reason to re-register turns one deliberate revocation into a
registration storm. A `409` must force a config resend and then resume. A `Retry-After`
must actually be waited out rather than replaced by our own backoff. None of that is
reachable from a server that only says yes.

So it is written rather than generated, standard library only, and the control channel
is the point of it:

```
POST /_control/enqueue   {"endpoint":"detection","status":429,"retry_after":30}
POST /_control/levels    {"streaming_allowed": false}
GET  /_control/requests
```

Requests are validated against the same generated models the client builds them with, so
a malformed payload is rejected the way the real server would reject it. One test posts
a `NodeConfig` serialised with `exclude_none=True` and watches the mock return `400` —
the serialisation bug, demonstrated against something that behaves like the server
rather than against an assertion we wrote ourselves.

It speaks HTTP/1.1 so keep-alive works; the client holds one connection open across
every request, and a mock that closed each time would hide any bug in that.

It is deliberately **not** a simulator. Registration rate limits, operator reactivation and
the Mender acceptance sweep are server-side concerns a node can only observe as an
opaque `403`, so they are scripted rather than modelled — the node behaves identically
either way, and modelling them would invent detail the spec withholds on purpose.

#### Driving it from a real node

The mock binds `127.0.0.1` and should stay that way. To have the Owl node talk to it,
use an SSH **reverse** tunnel rather than exposing a port:

```
ssh -R 18080:127.0.0.1:18080 owl      # node then reaches it at 127.0.0.1:18080
```

Verified 2026-08-10: a registration sent from the node arrived and was recorded. Three
things this sidesteps, all of which block the direct approach — the mock binding
loopback, the dev machine sitting behind WSL2's NAT, and the node needing a route back.
Nothing is exposed to any network and the tunnel dies with the session.

Two details that matter:

- **Not port 8080** — tar1090 already holds it on the node, and the forward fails with
  `remote port forwarding failed`. 18080 is clear.
- `--network host` on the container means `127.0.0.1` inside it *is* the host's
  loopback, so the tunnel is reachable from the container too, not just from a shell.

What this cannot exercise is **TLS**. The spec is HTTPS-only through Cloudflare, so the
handshake, certificate handling and Cloudflare's idle timeout on the kept-alive
connection (Q14) stay untested until there is a real endpoint.

### Stage 3b — lifecycle and gating

The state machine the thread diagram implies but does not show:

```
opted out ──▶ no identity ──▶ unregistered ──▶ registering ──▶ registered
                                                                 ├─▶ streaming
                                                                 └─▶ paused
```

- Token store at 0600 under `/data`, surviving an OS update.
- Opaque-403 discipline: honour `Retry-After`, jittered backoff, no hot loop. The
  server's per-`node_id` limit is 5/hour and 20/day, so a broken retry loop burns the
  budget in minutes.
- Opt-out is a first-class state: the container runs, reads the flag, idles, and keeps
  the status document fresh. It does not exit and is not removed from compose.
- Reflash needs no node-side work, but document it — a token-less board with a known
  identity sits in `403` until an operator opens a window, and the logs will not say so.

### Stage 3c — the two disciplines

- **Must-land** — register, config, heartbeat. Retry until they land.
- **Latest-wins** — detections. One in flight, abandon on timeout, drop freely.
- Handle `config_stale`, `409`, and surface `400` on config to retina-gui: a rejected
  config means the node cannot stream at all (Q12), so it must reach the operator.

If an early running service is wanted before detections are ready, take heartbeat
through 3c first — it is independent of the detection path and the more valuable half.

### Stage 4 — packaging

- Multi-arch image to ghcr.io, `retina-telemetry:vX.Y.Z`.
- Add to `retina-node/docker-compose.yml` with `${TELEMETRY_V}` following the existing
  pattern; `network_mode: host`, no published ports, config read-only, `/data`
  read-write for the token, image-tag env vars in for `versions`.
- Version file and tag conventions per the cross-repo release runbook.

---

## Decisions already made

| Decision | Why |
|---|---|
| Python 3, threads | matches retina-gui; three loops and one shared slot |
| Staged by layer, not endpoint | the shared retry/auth/level machinery needs one home, not four |
| Units convert in stage 2 only | keeps `data-sources.md` an accurate description of stage 1's output |
| Units in boundary field names | `delay_km` → `delay` makes a missing conversion visible at the call site |
| Poll `/api/detection` rather than tap TCP | latest-wins transport matches a latest-value API exactly, and it needs zero changes to blah2 or blah2-api |
| No spool for detections | the spec's transport model forbids it |
| Own `node_id` reader | retina-gui's returns `'Unknown'` on failure; that must never reach a payload |
| Assert array lengths in `collect/`, not `wire/` | blah2 guarantees it by construction but validates nothing; source coherence needs no server knowledge, and a malformed frame should never reach the slot |
| Liveness derived in `collect/blah2.py` | it needs the poll and the CPI, both of which live in stage 1; the wedged case is invisible to anything watching container state |
| Payload models generated from the spec | the spec is someone else's contract and drift is the failure mode; generation makes "the spec is the scope" mechanical, and brings the spec's own constraints along — `node_id="Unknown"` and `config_version=0` are rejected at construction without anyone remembering |
| Payloads serialised with `model_dump(mode="json", exclude_none=True)` | `mode="json"` is load-bearing for the acceptance timestamps; `exclude_none` is safe only while no spec field is required-and-nullable, which `tests/wire/test_payload_encoding.py` guards |
| A local state vocabulary, mapped to the wire's | the spec's `NodeState` is a closed set of five; ours has ten because the status document can report things a node with no token cannot say to the server at all |
| No consent record is ever synthesised | a missing record means the owner was not shown that text. The server may default an absent publication choice to `public` — that is its decision about its own archive, not licence for us to invent an acceptance |
| `uptime_s` is the device's | the heartbeat is the node's account of itself and "the node" is the board. blah2 reports its own at `/api/timing` and this process knows its own; neither is what the field means |
| A written mock, not a generated one | a generated mock always cooperates, and every behaviour worth testing in stage 3 is the server refusing |
| No docker socket | liveness falls out of the detection poll; versions come from compose env |
| Telemetry is opt-in | explicit user action in the setup wizard; also answers Q2's "is a silent node intended" with a deliberate yes |
| Outward status document | three failure modes must reach the operator, and we bind no ports |
| Build against a generated mock from stage 0 | the two blocking questions are upstream of us and would otherwise stall the whole build |

## Still to settle

Not blocked on anyone else — these are ours to decide, and worth deciding before the
stage they land in.

| Question | Lands in | Options |
|---|---|---|
| Missing `node_id`: exit non-zero, or hold and re-check? | stage 3b | crash-loop is honest but noisy; holding keeps the status document fresh |
| Status document path and format | stage 3b | retina-gui reads it; needs a contract either way |
| **Own compose project, or a service in `retina-node`?** | stage 4 | see below |

### The compose placement question

The plan has assumed this slots into `retina-node/docker-compose.yml` like every other
service. That is in tension with the architectural rule, and the tension is concrete:
retina-gui applies configuration with

```
docker compose -p retina-node up -d --force-recreate
```

(`retina-gui/src/routes/mode.py:144`) — project-wide. So every service in the radar
project is destroyed on every config change, including the one that should be reporting
that change. It also resets `seq`, which is Q10's discontinuity.

Not fatal: the restart is seconds, and on start we would notice the config hash moved
and `PUT` anyway, so it self-heals. What is lost is the ability to distinguish "node
restarting for a config change" from "node fell over" — and a config apply is exactly
when a node is likely to break.

**Decided: a service inside `retina-node`.** Not its own compose project, and not a host
service. Settled 2026-08-10 by reading Mender's docker-compose update module on the Owl
node, which answers both checks below.

Worth knowing that the obvious precedent is not the one it looks like. Verified on the
Owl node (2026-08-06): **retina-gui is not a container at all.** It is
`retina-gui.service`, a systemd unit running `/opt/retina-gui/src/app.py` directly on
the host. There is exactly one compose project — `retina-node`, seven containers —
brought up by `retina-node.service` as a oneshot. So retina-gui escapes the
force-recreate by being outside Docker entirely, not by being a second project, and it
is not evidence that a second project would work.

Both checks came back against a separate project:

**Mender's OTA path recreates project-wide too.**
`/usr/share/mender/modules/v3/docker-compose` runs
`docker compose --project-name X down` followed by `up`. So the teardown is not unique to
a config apply — an OS or stack update does the same thing, and no placement avoids it.

**One docker-compose artifact per device.** The module's `PERSISTENT_STORE` is a single
global path, `/data/mender-docker-compose`, holding one `current/`. A second compose
project is therefore not a second compose file but a *second Mender artifact* competing
for the same store, which would replace the first. That is not a cost, it is a
prohibition.

So `deploy/` in the tree above is not needed after all: the entry goes in
`retina-node/docker-compose.yml` alongside every other service.

**What that costs, stated plainly.** Every config apply and every OTA update recreates
this container along with the rest of the stack, so it stops reporting for the few
seconds that takes, and `seq` resets. What is lost is the ability to distinguish "node
restarting for a config change" from "node fell over".

Mitigating it is cheap and belongs in retina-gui rather than here: the container is
already exempt from `depends_on`, and the restart could exclude it by name
(`docker compose up -d --force-recreate --no-recreate retina-telemetry` does not compose
cleanly, but `docker compose restart` on the specific services would). That is a small
change to `routes/mode.py`, not a reason to fight Mender's deployment model.

## Deliberately not doing

- IQ upload. Not in the spec, not in scope.
- Track events. Decided rather than deferred: this service does not communicate
  tracks, so there is one transport discipline rather than two (Q8).
- A downlink command channel.
- Log shipping. If that is ever wanted, use Vector or the OTel Collector rather than
  growing it here — those solve log transport properly and this service should stay
  domain-shaped.
