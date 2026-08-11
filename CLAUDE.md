# retina-telemetry

The node-side telemetry uplink for the RETINA passive radar fleet. One container per
node, owning everything sent to the server: registration, detection streaming,
heartbeat, config sync. Nothing else on the node talks to `api.retina.fm`.

**Status: all five stages built and running.** Verified end to end on the Owl node
against a tunnelled mock, including the refusal paths. Read `docs/` before writing
anything — the reasoning behind most of the code is there rather than in the code.

## Read these first

| Doc | What it holds |
|---|---|
| `docs/node-ingest-v1.yml` | The server contract. Read-only input — proposed changes go in open-questions, not here |
| `docs/data-sources.md` | Verified facts about where node data comes from, its units, and its gotchas. Do not re-derive these |
| `docs/open-questions.md` | Awaiting answers from the server author. Two are blocking; the 2026-08-10 spec revision answered Q9 and enlarged Q2 |
| `docs/implementation-plan.md` | Architecture, module layout, the three build stages |

## The build is staged by layer, not by endpoint

| Stage | Owns | Knows about |
|---|---|---|
| **1 — Collection** | the four interfaces to the rest of the node stack | the node only |
| **2 — Construction** | turning what we collected into wire payloads | both sides |
| **3 — Communication** | `3a` machinery, `3b` lifecycle, `3c` the two disciplines | the server only |

The rule that keeps it honest: **stage 3 knows nothing about radar, stage 1 knows
nothing about the server, stage 2 is the only place that knows both.** A bistatic delay
in `comms/`, or an OpenAPI type in `collect/`, means the boundary has leaked.

Package layout is `collect/` → `wire/` → `comms/`, with `state.py`, `status.py` and
`errors.py` at the top level. Not `build/` — it is in `.gitignore`.

Verified on a node by `tools/live-probe.sh` (stage 1 and 2), `tools/live-service.sh`
(the whole service) and `tools/live-failures.sh` (the server's refusals, driven through
the mock's control channel).

Corollary: **all unit conversion happens in stage 2.** Stage 1 hands over source units
under names that say so — `delay_km`, `timestamp_ms`, `rx_alt_m` — and stage 2 emits the
spec's names and units. A missing conversion is then visible at the call site.

## Two blocking questions

Registration cannot be populated until these land (`docs/open-questions.md` Q1, Q2):

1. `beam_width_deg` / `beam_azimuth_deg` are required by the spec and **do not exist
   anywhere** in the node stack.
2. `RegisterRequest.agreements` needs **three** persisted records — `licence`,
   `remote_management` and `publication`. The EULA is a placeholder and the wizard
   checkbox persists nothing. `publication` is a privacy decision, not a form field:
   it governs whether a dwelling's position reaches a public archive, and its
   `version` records which disclosure wording the owner saw.

Both land in stage 2's registration payload. Everything else is buildable against a mock
generated from the OpenAPI spec — and stages 1 and 2 need no server at all, not even a
mock.

## Sibling repos

All at `/home/joshp/retina/`, all separate git repos:

| Repo | Relevance |
|---|---|
| `blah2-arm` | The radar itself (C++) plus `api/` (Node). Source of detections |
| `retina-node` | `docker-compose.yml` for the whole node stack, and the config defaults |
| `retina-gui` | On-node web UI, config authority, setup wizard, Mender/device state |
| `retina-tracker` | Tracking sidecar. Out of scope pending Q8 |
| `owl-os` | Ansible OS build. Owns the Mender identity script |

## Facts that are easy to get wrong

Full detail and citations in `docs/data-sources.md`. The short version:

- **`delay` from blah2 is in kilometres**, not microseconds. The spec wants µs
  (× 3.335641). Source precision is 2 dp in km ≈ 10 m.
- **`timestamp` from blah2 is epoch milliseconds**, integer. The spec wants float
  seconds.
- **Altitudes in node config are metres.** The spec wants feet (× 3.28084).
- **`t` is the *end* of the capture window**, not the start or middle.
- **`node_id` comes only from `/data/mender/node_id`.** Never derive it. retina-gui's
  `get_node_id()` returns the string `'Unknown'` on failure — do not reuse it. The
  `network.node_id: "ret000000000"` in `retina-node/config/default.yml` is a
  placeholder that fails the spec's pattern; ignore it.
- **`adsb_hex` is not blah2's.** blah2-api adds it, only when ADS-B is enabled, as
  objects rather than hex strings.
- **Detections are latest-wins.** No spool, no queue, at most one request in flight.
  Dropped frames are correct behaviour, not a bug to fix.
- **"Cloud services" in retina-gui means Mender, not telemetry.** The
  `cloud-services-disabled` flag toggles OTA. It is not a telemetry opt-in and there
  isn't one yet — but read it anyway: no Mender means registration sits in `403` forever.
- **Nothing in the stack pushes to us.** No event bus, no inbound ports. Every input is
  a poll or a file read, including "the user changed the config".
- **`wire/models.py` is generated.** Regenerate with `tools/generate-models.sh`; never
  hand-edit it, and `--check` will catch you.
- **`NodeState` on the wire is a closed set of five.** Our local vocabulary is richer
  because the status document can report things a node with no token cannot say at all;
  `NodeState.wire` maps between them. Never send a local value directly.
- **No consent record is ever synthesised.** A missing one means the owner was not shown
  that text. The server defaulting an absent publication choice to `public` is its
  decision about its own archive, not permission for us to invent an acceptance.
- **Never serialise a payload with `exclude_none=True`.** Use `wire.to_wire`.
  `beam_azimuth_deg` is required *and* nullable — `null` means omnidirectional — so
  dropping it produces a payload the server rejects, and it is nested inside
  `RegisterRequest`. It is the only such field in the spec, which is exactly why it gets
  missed.

## Conventions

- Slots into `retina-node/docker-compose.yml` like every other service: pinned image
  tag via `${TELEMETRY_V}`, config mounted read-only, `restart: always`.
- **No `depends_on` gated on blah2 health.** This service must keep reporting while the
  rest of the stack is crash-looping; that is the whole point of it.
- `network_mode: host` to reach blah2-api on `127.0.0.1:3000`. Bind no listening ports.
- The bearer token lives under `/data` at mode 0600 so it survives an OS update.
- **No docker socket.** blah2/ADS-B liveness is derived from the detection poll, and
  `versions` come from the compose image-tag env vars. Don't reach for the socket.
- Binding no ports means a **status document under `/data`** is the only way a failure
  reaches the operator. Three need to: no identity, a revoked token, a rejected config.

## Working agreements

- Nothing gets deployed to a live node without Josh's express sign-off.
- The OpenAPI spec is someone else's contract. Disagreements go to
  `docs/open-questions.md` for them to answer; do not edit the spec to match the code.
- **The spec is the scope.** If a field is not in it, we do not collect it — however
  cheap or obviously useful it looks. Wanting something new means an open question to
  the server author, not a field we add unilaterally. This has already removed Pi
  throttle flags, `/api/timing`, the capture status endpoints and the ADS-B association
  tolerances; `docs/data-sources.md` §5 keeps the list and the reasons, so nobody
  re-derives them.
- Collecting something the spec does not send is justified **only** when a required
  field depends on it. Today exactly one thing qualifies: `delay_max_bins` is collected
  because `max_range_km` is derived from it. Say which required field, in a comment, at
  the point of collection. Two earlier examples died — `cpi_s` seeded a staleness window
  that no longer exists, and `truth.adsb.enabled` turned out to be redundant because the
  presence of the `adsb` key *is* the flag.
