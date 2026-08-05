# Open questions

Awaiting answers from the author of `docs/node-ingest-v1.yml`. Sent 2026-08-04.

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

### Q2 — no agreement record exists to send

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

## OPS

### Q9 — cadence is "server-issued" but nothing carries it

Both `DetectionAck` and `HeartbeatResponse` lack a `detection_hz` /
`heartbeat_interval_s`. Hardcode 2 Hz / 60 s for v1, or add the fields now?

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
