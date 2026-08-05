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
`ingress/`, the boundary has leaked. This is worth enforcing in review — it is what
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
operator to open a reflash window. So the supervisor gates thread startup rather than
letting three loops spin against a `None` token. Only the status writer runs
unconditionally, because a node that cannot register is precisely the node whose
operator needs to be told why.

### Module layout

```
retina_telemetry/
  __main__.py            wiring, signal handling, thread lifecycle
  state.py               token store (0600), seq/boot_id, node_ref, config_version
                         — the concurrency boundary; threads touch shared state only here

  ingress/               ── stage 1 ──
    blah2.py             blah2-api client: /api/detection, /api/timing, capture status
    node_config.py       config.yml read + change hashing (no conversion)
    identity.py          /data/mender/node_id reader — raises, never defaults
    consent.py           opt-in + agreement record reader
    host.py              cpu / temp / disk / throttle from /proc, /sys, statvfs
  egress/                ── stage 1, outbound half ──
    status.py            status document for retina-gui

  build/                 ── stage 2 ──
    units.py             km→µs, ms→s, m→ft, max_range_km derivation
    detection_frame.py   raw poll → DetectionFrame: length assert, adsb_hex normalise
    node_config_map.py   config.yml → NodeConfig
    heartbeat.py         health + state + versions + errors → HeartbeatRequest
    registration.py      → RegisterRequest (two fields blocked: Q1, Q2)
    errors.py            bounded error accumulator for the heartbeat errors[] field

  comms/                 ── stage 3 ──
    client.py       3a   session, auth, backoff, Retry-After, clock offset
    levels.py       3a   shared response-level applier
    lifecycle.py    3b   state machine, registration gating, opt-out
    reliable.py     3c   must-land: register, config, heartbeat
    stream.py       3c   latest-wins: detections
```

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
- **Mock server from the spec.** Needed from 3a onward; stages 1 and 2 need no server
  at all, not even a mock.

### Stage 1 — collection

The four incoming interfaces and the one outgoing one. Full inventory in
`docs/data-sources.md` §0.

- `blah2.py` — poll `/api/detection` at ~4 Hz against a 2 Hz producer, dedupe on
  `timestamp`. Also `/api/timing` for CPI overrun, and the capture status endpoints.
- `node_config.py` — read the read-only mount, hash for change detection.
- `identity.py` — the hard-error behaviour, and a test that `'Unknown'` can never
  appear in a payload.
- `consent.py` — opt-in state and the agreement record (format still to agree with
  retina-gui; see Q2).
- `host.py` — cpu, temp, disk, throttle flags.
- `egress/status.py` — the document retina-gui reads.

**Deliverable:** a process that can report everything it can see about the node and
nothing about the server. No network calls off-box.

**No docker socket.** blah2 and ADS-B liveness fall out of the detection poll, and
`versions` come from compose env vars. Dropping the socket removes a mount from the one
container on the node that talks to the internet.

### Stage 2 — construction and packaging

- `units.py` with property tests: km→µs, ms→s, m→ft, `max_range_km` derivation. These
  are the highest-value tests in the repo — three conversions where a silent error is
  plausible and invisible on the wire.
- `detection_frame.py` — assert equal array lengths, synthesise `adsb_hex` nulls when
  ADS-B is off, map association objects down to `.hex`, attach `seq` and
  `config_version`.
- `node_config_map.py`, `heartbeat.py`, `registration.py`.
- Every output validated against the spec's schemas.

**Deliverable:** given stage 1's output, valid wire payloads. Still no network.

**Blocked, and scoped around:** the registration payload needs `beam_width_deg` /
`beam_azimuth_deg` (Q1) and the agreement record (Q2). The other eleven `NodeConfig`
fields, the detection frame and the heartbeat body are all unblocked. Build the seam,
leave the two fields open.

### Stage 3a — the machinery

Session, auth, jittered exponential backoff, `Retry-After`, clock offset against
`server_time`, and the shared level applier. Built once, used by all four endpoints.

Testable in full against a scripted fake before any endpoint logic exists.

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
| Assert array lengths at the boundary | blah2 guarantees it by construction but validates nothing |
| No docker socket | liveness falls out of the detection poll; versions come from compose env |
| Telemetry is opt-in | explicit user action in the setup wizard; also answers Q2's "is a silent node intended" with a deliberate yes |
| Outward status document | three failure modes must reach the operator, and we bind no ports |
| Build against a generated mock from stage 0 | the two blocking questions are upstream of us and would otherwise stall the whole build |

## Still to settle

Not blocked on anyone else — these are ours to decide, and worth deciding before the
stage they land in.

| Question | Lands in | Options |
|---|---|---|
| Payload models: generated from the spec, or hand-written? | stage 0 | codegen + CI drift check, hand-written, or hand-written with a schema test |
| Mock: Prism, an in-process fake, or both? | stage 0 / 3a | Prism validates shape; only a scriptable fake can force 401/409/429 sequences |
| Missing `node_id`: exit non-zero, or hold and re-check? | stage 3b | crash-loop is honest but noisy; holding keeps the status document fresh |
| Status document path and format | stage 1 | retina-gui reads it; needs a contract either way |
| Where the opt-in / agreement record lives | stage 1 | retina-gui writes, we read; whose `/data` subtree, and whose docs describe it |
| `state` vocabulary | stage 2 | ours (`streaming`, `paused`, …) with retina-gui's `updating_*` folded in, or theirs |

## Deliberately not doing

- IQ upload. Not in the spec, not in scope.
- Track events, pending **Q8**.
- A downlink command channel.
- Log shipping. If that is ever wanted, use Vector or the OTel Collector rather than
  growing it here — those solve log transport properly and this service should stay
  domain-shaped.
