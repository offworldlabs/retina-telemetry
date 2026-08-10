# Open questions

Awaiting answers from the author of `docs/node-ingest-v1.yml`. Sent 2026-08-04.
**Revised spec received 2026-08-10** — what it answered is marked below.

Status: **BLOCKING** = cannot ship a registering node without it. **WIRE** = changes
the payload, cheap now and expensive after nodes are in the field. **OPS** = can be
decided during the build.

---

## BLOCKING

### Q1 — `beam_width_deg` / `beam_azimuth_deg` do not exist anywhere

Zero hits across owl-os, retina-node, retina-gui, blah2-arm. `NodeConfig` marks both
required, so registration cannot be populated. This is new config fields + GUI form +
wizard step, not a mapping.

**Proposal:** are current nodes all effectively omnidirectional? If so, ship v1 with a
sensible default `beam_width_deg` and `beam_azimuth_deg: null`, and add real UI once
directional installs exist. Needs confirmation that a placeholder width is not worse
than useless to the solver.

### Q2 — no agreement records exist to send

**The 2026-08-10 revision made this larger, not smaller.** One `agreement` became three
separately versioned records — `licence`, `remote_management` and `publication` — and
the third is a product decision rather than a form field.

`publication` carries `{version, accepted_at, choice}` where choice is `public` or
`private`, and the spec is blunt about the stakes: publication is irreversible in the
sense that matters, "the receiver's position is recoverable from the measurements
whether or not the coordinate columns are published", and the disclosure "has to say
plainly that the dwelling's position is published". Its version records which wording
each owner saw.

So the wizard now needs three pieces of versioned text and three recorded decisions, not
a checkbox. Nothing on the node can manufacture any of them.

#### The original question, still open

`RegisterRequest.agreement` requires `{version, accepted_at}`. On the node today the
wizard has an `agreements` step and an `/eula` page, but the EULA text is explicitly a
placeholder and the checkbox only enables the Continue button. **Nothing is persisted.**

Needs: real EULA text, a version string and its format, persisted acceptance under
`/data`.

**Partly resolved node-side (2026-08-05).** Telemetry will be opt-in via an explicit
action in the setup wizard, and that opt-in record and the agreement record are the same
artifact — written by retina-gui, read by this service. An opted-out or un-accepted node
runs the container, idles without registering, and reports why in its status document.

So the sub-question "an un-accepted node is silent, is that the intent?" is answered:
yes, deliberately. What remains for the server author:

- If the agreement version bumps later, does the server want a re-accept, and how does
  it signal that? There is no control field for it today, and `config_stale` is the only
  existing precedent for the server asking a node to resend something.

---

## WIRE

### Q3 — timestamp window edge, and `cpi_s`

`t` is currently the **end** of the capture window (see `data-sources.md` §2). The spec
says "the capture time of the CPI", which is ambiguous. Two asks:

1. State which edge — start, centre, or end.
2. Add **`cpi_s` to `NodeConfig`**. The offset scales with `cpi`, which is per-node
   config the server never sees, so it currently cannot correct for it. The same field
   also makes capture gaps detectable (`t[n+1] - t[n] > cpi_s`), which nothing else
   reveals — `seq` stays contiguous through capture loss.

Not asking for sub-CPI resolution. 0.5 s is the practical floor and ±0.25 s against a
4 s association window is fine.

### Q4 — units: `delay` and altitudes

- `delay`: spec says microseconds, blah2 natively produces **kilometres** of bistatic
  range. Conversion is `µs = km × 3.335641`. Would you rather receive km, given that is
  the source unit and the source precision is 2 dp (≈10 m ≈ 0.033 µs)? Sending µs at
  more decimal places implies precision that does not exist.
- Altitudes: spec says feet, node config is **metres**, everything else in the spec is
  SI. Why feet? A silent metres-read-as-feet puts a transmitter ~500 ft low with no
  error anywhere.

### Q5 — `tx_callsign` is a display name

Available as `location.tx.name`, e.g. `"Crystal Palace"` — free text the operator typed
in the tower step, not a regulatory callsign. Tower-Finder holds real callsigns and
could be plumbed through. Which do you want?

### Q6 — `max_range_km` derived rather than stored

Not a config field on the node, but computable as
`process.ambiguity.delayMax × c / fs / 1000` — 60 km at the current 400 bins / 2 MHz.
Deriving means it can never disagree with what blah2 actually computes. Agreed?

### Q7 — `adsb_hex` is a per-node hypothesis

Association is a tolerance-gated single-best match done in blah2-api, and both
tolerances are node config (`delay_tolerance: 2.0`, `doppler_tolerance: 5.0`). So
strictness varies node to node, and cross-node association would be comparing
hypotheses made under different thresholds.

**Proposal:** either carry the residuals (blah2-api already computes them) or put the
two tolerances in `NodeConfig` so the server knows what it is comparing.

### Q8 — detections only: where do track events go?

The node already runs retina-tracker, producing track lifecycle events
(`track_id`, `adsb_hex`, `timestamp`, `length`, `detections[]`, `is_anomalous`,
`anomaly_types[]`) as JSONL. Is all tracking moving server-side into retina-analytics,
with retina-tracker staying local purely for GUI preview and Auto-Calibrate?

Asking because the answer decides whether this container is built detections-only or
with a second, lower-rate, must-not-lose stream from day one. Retrofitting that later
is the expensive path.

---

### Q15 — `board_model` will not look like your example

Your example is `"raspberrypi5-4gb"`. We are sending the **Mender device type**, read
from `/data/mender/device_type`, which on a current node is:

```
pi5-v3-arm64
```

Same shape — a device-type slug rather than a hardware description — but a different
vocabulary. We picked it because Mender targets artifacts by device type, so it is the
string that decides which software a board is allowed to receive, which makes it the
more useful diagnostic. The alternative was `/proc/device-tree/model`
(`"Raspberry Pi 5 Model B Rev 1.1"`), which carries a board revision that means nothing
to either of us.

Flagging it only because if you are parsing or grouping on this field, the values will
not be what the example implies. The field is free text in the schema, so nothing breaks
either way. If you would rather have RAM size or hardware revision in there, that is a
second field rather than a different source — the device type does not carry them.

### Q16 — `config_version` makes an unconditional heartbeat impossible

**Unchanged by the 2026-08-10 revision.** `config_version` is still required on
`HeartbeatRequest` and the description still says the beat is sent "unconditionally",
so the contradiction stands.

`POST /nodes/heartbeat` says it is sent *"from process start until shutdown,
**unconditionally**: whether or not detections are flowing, whether or not the last
frame was empty, and whether or not `streaming_allowed` is false."*

But `HeartbeatRequest` marks `config_version` **required**, and that value is yours —
the node only learns it from a registration or a `PUT /nodes/config` response. So there
is no heartbeat a node can send before it has one, and "unconditionally" cannot be
honoured literally.

It bites in two places:

1. **Every restart.** We persist only the bearer token, since `config_version` is
   re-obtainable from a `PUT /nodes/config` and the token is not re-obtainable from
   anything (see `docs/implementation-plan.md`). So a restarted node is silent until
   that PUT lands — a second or two, normally.
2. **A node whose `config.yml` is unreadable**, which is permanent. It cannot build a
   `NodeConfig`, so it cannot PUT, so it never gets a `config_version`, so it never
   heartbeats. A node that cannot report its configuration is exactly the node worth
   hearing from, and it is the one that goes quiet.

**Proposal: make `config_version` optional on `HeartbeatRequest` only.** Its absence
would mean "I have not been told one yet", which is information rather than a gap — and
it would let the sentence in the description be true. It stays required on
`DetectionFrame`, where a frame genuinely cannot be filed without it.

Caching the last known version on disk was the alternative and we rejected it: it means
a node can report a `config_version` the server has since replaced, and it adds durable
state whose only purpose is to paper over this.

---

## OPS

### Q9 — cadence is "server-issued" but nothing carries it — **ANSWERED**

The 2026-08-10 revision makes both rates fixed rather than server-issued: "2 Hz, fixed"
and "60 s, fixed" in the endpoint table. No field is needed and none was added.

It also added a requirement we did not have: *"Apply a uniform random phase offset
within the interval, so that a fleet restarting together does not settle into one bucket
and post simultaneously every minute."* Implemented in `__main__.heartbeat_loop`.

### Q10 — `seq` across restarts

Container restarts and the counter resets. Gap or reset, from the server's side?
Persisting it costs an fsync per frame at 2 Hz on an SD card unless checkpointed
coarsely. **Proposal:** a `boot_id` alongside a restart-local `seq`, which makes the
discontinuity explicit and costs one write per boot.

### Q11 — `queue_depth` is meaningless under latest-wins

At most one request in flight and no queue, by design. Report 0/1, drop the field, or
did it mean something else?

### Q12 — a `400` on config permanently blocks streaming

Retrying unchanged will not help, so the node sits unstreamable until a human edits
config. Intended? And does `Error.detail` carry enough for retina-gui to tell the
operator which field to fix?

### Q13 — is any detection history durable, or is this strictly live?

Latest-wins makes gaps permanent by design. Fine for live tracking; not fine if anyone
later wants a complete archive. Worth knowing now, because it is the difference between
"no spool" and "spool".

### Q14 — Cloudflare specifics

Any WAF bypass rule for node traffic, and what is the idle timeout on the kept-alive
connection? A 60 s heartbeat should keep it warm, but confirmation beats discovery.

---

## Answered

### Are the four arrays genuinely parallel and equal-length? — YES

Equal-length by construction; see `data-sources.md` §1. Caveat: nothing validates it,
so the agent asserts at the boundary. The fourth array, `adsb_hex`, is not blah2's — it
comes from blah2-api and only when ADS-B is enabled; the agent synthesises `[null] * n`
otherwise and maps objects down to `.hex`.

Nothing to exclude. One thing missing: no way to distinguish "no detections this CPI"
from "detector disabled" — both are empty arrays.

### What happens when `/data/mender/node_id` is absent? — hard error is correct and safe

A MAC fallback exists but emits the attribute key `mac=`, not `node_id=`, and never
writes the file. It cannot masquerade as a `ret…` node. Full trace in
`data-sources.md` §3, including two landmines: retina-gui's `get_node_id()` returns the
string `'Unknown'` (do not reuse), and `default.yml` carries a
`network.node_id: "ret000000000"` placeholder that fails the spec's pattern.


---

## Raised by the 2026-08-10 revision

### Q17 — is `starting` or `error` right for a radar that has stopped?

`NodeState` is now a closed set of five, which is a clear improvement over free text.
Two of our four reportable states had no exact equivalent, so we map them:

| Our situation | We send | Why |
|---|---|---|
| Registered, blah2 has never produced a frame | `starting` | Your description: "the window before the radar has produced anything" |
| Registered, blah2 produced and then stopped | `error` | A working node with a broken radar, which is not a beginning |
| Token refused by the server | `error` | Streaming has stopped and cannot resume without intervention |

The middle row is the one we are least sure of. `error` may read as "the node is
broken" when the node is fine and blah2 is not — and `health.blah2: "down"` already
says which. If you would rather that case reported `starting` too, or if `error`
should be reserved for something else, say so; it is one line.

We never send `stopping`. A final heartbeat during shutdown would mean a network call
on the way out, which can hang; the status document records it locally instead. Happy
to add it if you would rather see it.

### Q18 — `tx_callsign`: your example changed, and ours has a space

Your example moved from an unset value to `"CRYSTAL_PALACE"`, which reads as a
convention. We send `location.tx.name` verbatim, which on a real node is
`"Crystal Palace"` — free text the operator typed in the tower step.

Q5 already asked which you want. If the underscored form is a convention rather than an
example, say so and we will normalise; if you want real regulatory callsigns,
Tower-Finder holds them and that is a retina-gui change.
