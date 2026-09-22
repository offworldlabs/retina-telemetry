# retina-telemetry

The node-side telemetry uplink for the RETINA passive radar fleet. One container per
node, owning everything sent to the server: registration, detection streaming,
heartbeat, config sync. Nothing else on the node talks to `api.retina.fm`.

**Status: built, and implementing spec v1.4.0.** Verified end to end on the Owl node
against a tunnelled mock — every endpoint, every reachable state including `stalled`,
and the refusal paths.

## Read these first

| Doc | What it holds |
|---|---|
| `docs/node-ingest-v1.yml` | The server contract. Read-only input — never edited to match the code |
| `docs/data-sources.md` | Verified facts about where node data comes from, its units, and its gotchas. Do not re-derive these |

Two docs, deliberately. An earlier plan and a running list of open questions were
deleted once the service was built: the plan described stages that no longer exist, and
the questions had all been answered or withdrawn. Correspondence with the server author
lives outside version control (see `.gitignore`), because a draft that gets rewritten
between sends reads as a second, competing spec.

## The build is staged by layer, not by endpoint

| Stage | Owns | Knows about |
|---|---|---|
| **1 — Collection** | the four interfaces to the rest of the node stack | the node only |
| **2 — Construction** | turning what we collected into wire payloads | both sides |
| **3 — Communication** | request machinery, lifecycle, the two traffic disciplines | the server only |

The rule that keeps it honest: **stage 3 knows nothing about radar, stage 1 knows
nothing about the server, stage 2 is the only place that knows both.** A bistatic delay
in `comms/`, or an OpenAPI type in `collect/`, means the boundary has leaked.

Package layout is `collect/` → `wire/` → `comms/`, with `state.py`, `status.py` and
`errors.py` at the top level. Not `build/` — it is in `.gitignore`.

Verified on a node by five scripts in `tools/`: `live-probe.sh` (stages 1 and 2),
`live-service.sh` (the whole service), `live-failures.sh` (the server's refusals),
`live-stress.sh` (restarts and a broken config) and `live-stalled.sh` (stops blah2 to
reach `stalled` — the only one that writes to the node). Watch any of them live at
`http://127.0.0.1:18080/`, served by the mock itself.

Run every CI gate locally with `tools/check.sh`, or `tools/check.sh --tracked` to run
against a clean copy of tracked files — which is what CI actually sees, and has caught
two false passes the working tree hid.

Corollary: **all unit conversion happens in stage 2.** Stage 1 hands over source units
under names that say so — `delay_km`, `timestamp_ms`, `rx_alt_m` — and stage 2 emits the
spec's names and units. A missing conversion is then visible at the call site.

## Four files retina-gui writes, two of which gate registration

All four live under `/data/retina-gui`, mounted read-only, and nothing in any of them
is ever synthesised here. **`telemetry-consent.json` and `setup-wizard-completed` gate
registration**: a node missing either refuses to register and says which in its status
document. `telemetry-contact.json` and `telemetry-claim.json` gate nothing and are
optional throughout.

**`telemetry-consent.json`** carries the three records `RegisterRequest.agreements`
needs: `licence`, `remote_management` and `publication`. `publication` is a privacy
decision rather than a form field: it governs whether a dwelling's position reaches a
public archive, and its `version` records which disclosure wording the owner saw. A
missing record means the owner was not shown that text. Shipped in retina-gui v0.7.0.

**`telemetry-contact.json`** carries the owner's contact details for
`PUT /nodes/contact`: first name, last name, email, phone and a two-letter
`country` for the phone number. Every field is optional and so is the file. A
node that has none never calls the endpoint, which the spec states explicitly,
so an absent file is a complete answer rather than a gap and nothing here
blocks on it. See `collect/contact.py`.

**`telemetry-claim.json`** carries the address that *owns* the node, for
`PUT /nodes/claim`, plus a `send_requested_at` stamp when the owner asks for their
link again. A different question from the contact email and a worse failure: the
server mails this one a link, and opening it binds the node to the account behind
it. Both keys optional, the file optional, and an unclaimed node runs exactly as a
claimed one does. See `collect/claim.py`. Shipped in retina-gui 2026-09-22.

**`setup-wizard-completed`** proves the config is the owner's rather than the shipped
default. `retina-node/config/default.yml` ships a *working* configuration (Greenwich
Observatory, Crystal Palace), the merger writes it on first boot, and retina-gui records
consent at wizard step 1 but the location at step 5. Without this gate we registered in
that window and told the server real nodes were at Greenwich. See `collect/wizard.py`.

**The flag postdates some nodes.** It only arrived in retina-gui aee29a6 (2026-06-24),
so nodes configured before then had none; retina-gui backfills it at startup from a
location in `user.yml`. That backfill must reach the fleet *before* this gate ships, or
every configured node still holding a flagless `/data` stops registering.

## Sibling repos

All at `/home/joshp/retina/`, all separate git repos:

| Repo | Relevance |
|---|---|
| `blah2-arm` | The radar itself (C++) plus `api/` (Node). Source of detections |
| `retina-node` | `docker-compose.yml` for the whole node stack, and the config defaults |
| `retina-gui` | On-node web UI, config authority, setup wizard, Mender/device state. **Owes us four things — see below** |
| `retina-tracker` | Tracking sidecar. Out of scope — decided, not pending. This service does not communicate tracks |
| `owl-os` | Ansible OS build. Owns the Mender identity script |

### What retina-gui owes this service

Nothing here is buildable from this repo, and the first one blocks every node:

| | Blocking | What |
|---|---|---|
| Backfill `setup-wizard-completed` onto pre-2026-06-24 nodes | **yes** | Ours gates registration on it. Nodes configured before the flag existed have none and would never register. Must be deployed to the fleet before this gate ships |
| Persist the three consent records to `/data/retina-gui/telemetry-consent.json` | shipped | Landed in retina-gui v0.7.0. Written at the agreements step, all three at once |
| Cap the tower-name field at 32 characters | shipped | `TX_NAME_MAX_LENGTH` in `config_schema.py`. The spec caps `tx_callsign` at 32 and the field was unbounded free text |
| Read `/data/retina-telemetry/status.json` | no, but | We bind no ports, so it is the only way *no identity*, *revoked token* and *rejected config* reach an operator. `telemetry_status.py` reads it and the home page shows it |
| Collect `location.rx.beam_width` / `beam_azimuth` | no | Deferred indefinitely. Both are nullable, so sending two nulls is correct behaviour rather than a gap |
| Collect the owner's contact details | shipped | Landed 2026-09-16. A skippable wizard step after the agreements step, plus a block under Remote support on the Configuration page, writing `/data/retina-gui/telemetry-contact.json` |
| Collect the address that **owns** the node | shipped | Landed 2026-09-22. A Node claim section on the Configuration page writing `/data/retina-gui/telemetry-claim.json`, with Save for the address and Send again for another link. Not the contact email: see `docs/data-sources.md` §4 |

`owl-os` separately owes a `mender-update show-provides` snapshot so
`versions.retina_node` has a source. Optional field; omitted honestly until then.

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
- **The contact document is sent on local change only.** Nothing on the server
  asks for it and no response marks it stale, so a change in the file is the
  only thing that sends it. It rides the config loop's tick because both are
  noticed by re-reading a file retina-gui wrote. **A node with nothing to
  report never calls the endpoint**: an empty document is a valid payload that
  *clears* what the server holds, which is right for an owner who deleted their
  details and wrong for one who never gave any.
- **A refused contact document goes to `errors[]` and never to `detail`.**
  Unlike a refused registration, it breaks nothing: the node registers, streams
  and beats exactly as before, and the only loss is a way to ring the owner.
  `detail` is for what stops a node working.
- **The claim's address comes from retina-gui and nowhere else.** Never the contact
  email: that answers "whom do we ring" and reusing it would mail a stranger a link that
  hands them the node. `telemetry-claim.json` carries the address and, when the owner
  presses send again, a timestamp. **Which call to make is decided here, not there**: a
  changed address is a `PUT` and a fresh timestamp is a `resend`, because this is the
  only side that knows what the server does with each.
- **Offering an address the node already holds does nothing at all.** It is accepted,
  writes nothing and mails nothing. A declined link leaves the node `unclaimed` *with
  the address still on file*, so it can sit there and no number of offers will move it;
  `POST /nodes/claim/resend` is the only way out. Checked against production on
  2026-09-22. This is why the resend trigger exists, and why a "claim this node" button
  that always PUTs is wrong.
- **A stale ask for a link is ignored**, past `CLAIM_ASK_FRESH_FOR_S`. Nothing durable
  records that we acted on one, so without an age bound every restart would mail the
  owner another link.
- **The three claim fields are read as one block, gated on `claim_state`.**
  `claim_email` is required *and nullable*, so a field-by-field read would let a
  detection ack, which carries none of them, blank an address the heartbeat reported a
  second earlier. None of it gates anything: an unclaimed node registers, streams and
  beats normally. **`pending` is not durable** and nothing should wait on it: a declined
  link can strand a node there for about fifteen minutes before it falls back to
  `unclaimed`.
- **Nothing in the stack pushes to us.** No event bus, no inbound ports. Every input is
  a poll or a file read, including "the user changed the config".
- **`wire/models.py` is generated.** Regenerate with `tools/generate-models.sh`; never
  hand-edit it, and `--check` will catch you. The spec is normalised on the way in by
  `tools/normalise_spec.py`, which rewrites the `anyOf: [X, null]` spelling the server's
  FastAPI export uses into the equivalent `type: [x, "null"]` one. Without it every
  nullable field becomes a `RootModel` wrapper needing `.root` at each read, which
  adopting v1.2.0 turned from 3 wrappers into 19. The checked-in spec is never touched.
- **`stalled` is confirmed.** It was shipped ahead of the server under v1.1.1, which
  marked it "Proposed, confirm before implementing"; v1.2.0 onwards carries all six
  values with no caveat. We send it for a radar that produced and then stopped, because
  the alternative, `error`, raises against the node when the fault is the radar's. The
  revert that was kept ready is no longer needed.
- **`NodeState` on the wire is a closed set of six.** Our local vocabulary is richer
  because the status document can report things a node with no token cannot say at all;
  `NodeState.wire` maps between them. Never send a local value directly.
- **No consent record is ever synthesised.** A missing one means the owner was not shown
  that text. The server defaulting an absent publication choice to `public` is its
  decision about its own archive, not permission for us to invent an acceptance.
- **Serialise payloads with `wire.to_wire`, never `exclude_none=True`.** A
  *required and nullable* field's `null` is a value the server expects rather than an
  absence, so dropping the key produces a payload it rejects. `to_wire` also applies
  `mode="json"`, which is load-bearing: without it the acceptance timestamps stay as
  `datetime` objects and `json.dumps` refuses the registration payload outright.
  **Fourteen fields are required-and-nullable in v1.4.0**, so payloads go out through
  `wire.to_wire`, never `model_dump(exclude_none=True)` directly.
  `tests/wire/test_serialise.py` pins the inventory by name and fails if the spec grows
  or loses one.
- **The antenna geometry is nullable, and null is the normal case.** retina-gui is not
  collecting `beam_width_deg` / `beam_azimuth_deg` from owners for the foreseeable
  future, so every node sends two explicit nulls. Nothing is ever substituted.
- **A node with no location registers anyway, with seven explicit nulls.** retina-node
  ships `location.*` null until an owner picks a tower. v1.2.0 made the six coordinates
  required-and-nullable and v1.2.2 did the same for `tx_callsign`, so such a node is
  counted and streamed from; it just places nothing on the map. `is_located` still
  exists but gates nothing: it feeds the status document, which is the only thing
  telling an operator why a node that looks entirely healthy contributes nothing.
  Registration was held under v1.1.1; that is over.
- **The server pairs a latitude with its longitude.** Both or neither; half a pair is a
  `400 invalid_config` naming the null half. Altitude is not paired with anything, and
  the degenerate-baseline check (receiver and illuminator at the same point) only
  applies once both ends have a position.
- **Owl runs `dopplerMin/Max: ±1000`, not the standard ±200.** Five times the Doppler
  bins per CPI, so its frame rate (0.6–0.9 Hz measured) is slower than a standard node's
  and is **not** a fleet figure. Do not quote it as one.

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

## Decisions, and why

Folded in from the implementation plan when that was retired. These are the ones
that get re-litigated if the reasoning is not written down.

| Decision | Why |
|---|---|
| Python 3, threads | matches retina-gui; five loops and one shared slot |
| Staged by layer, not endpoint | the shared retry/auth/level machinery needs one home, not four |
| Units convert in stage 2 only | keeps `data-sources.md` an accurate description of stage 1's output |
| Units in boundary field names | `delay_km` → `delay` makes a missing conversion visible at the call site |
| Poll `/api/detection` rather than tap TCP | latest-wins transport matches a latest-value API exactly, and needs zero changes to blah2 or blah2-api |
| No spool for detections | the spec's transport model forbids it |
| Own `node_id` reader | retina-gui's returns `'Unknown'` on failure; that must never reach a payload |
| Assert array lengths in `collect/`, not `wire/` | blah2 guarantees it by construction but validates nothing, and a malformed frame should never reach the slot |
| Liveness derived from the detection poll | the wedged case is invisible to anything watching container state |
| Payload models generated from the spec | drift is the failure mode; generation brings the spec's own constraints along, so `node_id="Unknown"` is rejected at construction without anyone remembering |
| A written mock, not a generated one | a generated mock always cooperates, and every behaviour worth testing in stage 3 is the server refusing |
| A local state vocabulary, mapped to the wire's | the status document can report things a node with no token cannot say to the server at all |
| `uptime_s` is the device's | the heartbeat is the node's account of itself, and "the node" is the board |
| No docker socket | liveness falls out of the detection poll; versions come from compose env vars |
| Outward status document | three failure modes must reach the operator, and we bind no ports |
| Normalise the spec into the generator, never in place | the server's FastAPI export spells nullable as `anyOf`, which `--collapse-root-models` cannot reach; the checked-in contract stays byte-identical to what was sent |
| One `tools/check.sh` | CI, release and a terminal disagreeing about "green" is how the release gate silently lost two checks |

## Working agreements

- Nothing gets deployed to a live node without Josh's express sign-off.
- The OpenAPI spec is someone else's contract. Disagreements go to them; do not edit the
  spec to match the code. **One exception exists**, and it has already bitten once: the
  beam fields were changed with the server author's agreement, relayed by Josh, and their
  next revision did not carry it, so our edit was silently reverted on adoption. **Check
  `NodeConfig.beam_width_deg` when adopting any revision**, and expect to reapply it.
  Checked on adopting `1.2.2` (2026-09-16) and `1.4.0` (2026-09-22): it survived
  both times, nullable as agreed. Keep checking anyway.
- **The spec is the scope.** If a field is not in it, we do not collect it — however
  cheap or obviously useful it looks. Wanting something new means asking the server
  author, not a field we add unilaterally. This has already removed Pi
  throttle flags, `/api/timing` and the capture status endpoints;
  `docs/data-sources.md` §5 keeps the list and the reasons, so nobody re-derives them.

  It works in both directions, which is the point. `cpi_s` and the two ADS-B association
  tolerances were removed under this rule and came back as **required** fields in v1.1.1
  — through the spec, because we asked, rather than because we decided they looked
  useful.
- Collecting something the spec does not send is justified **only** when a required
  field depends on it. Today exactly one thing qualifies: `delay_max_bins` is collected
  because `max_range_km` is derived from it. Say which required field, in a comment, at
  the point of collection. Two earlier examples died — `cpi_s` seeded a staleness window
  that no longer exists, and `truth.adsb.enabled` turned out to be redundant because the
  presence of the `adsb` key *is* the flag.
