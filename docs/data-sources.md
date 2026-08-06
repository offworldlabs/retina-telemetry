# Node data sources

Everything here was verified by reading the source on 2026-08-04. Line numbers are
against the working copies in `/home/joshp/retina/` at that date — re-check before
relying on an exact line, but the behaviour claims are what matter.

The point of this document is that a fresh session should not have to re-derive any
of it.

---

## 0. The interfaces, in full

Everything this service takes from the rest of the node stack. Nothing pushes to us —
we bind no ports, and there is no event bus. Every one of these is us pulling.

| # | Interface | Mechanism | Yields | §|
|---|---|---|---|---|
| 1 | blah2-api on `127.0.0.1:3000` | HTTP poll, keep-alive | detections, blah2 + ADS-B liveness, CPI overrun, RF status | §1, §5 |
| 2 | `/data/retina-node/config/config.yml` | read-only mount, hashed | all of `NodeConfig`, plus `cpi`, `delayMax`, `fs`, `truth.adsb.enabled` | §4 |
| 3 | `/data/mender/node_id` | read once at boot | identity | §3 |
| 4 | `/data/retina-gui/telemetry-consent.json` | read-only — **not written yet** | whether we may talk to the server at all | §4, Q2 |

One HTTP client, two file reads, one file still to be defined.

Outbound, in the same category: a status document this service writes for retina-gui
to read (`docs/implementation-plan.md`, stage 1). We bind no ports, so a file is the
only way three separate failure modes — no identity, a revoked token, a rejected
config — ever reach the operator.

**Not from the stack:** host metrics (`/proc`, `/sys`, `statvfs`), `uptime_s`, `seq`,
`boot_id`, and the bearer token, which is ours. `versions` come from compose env vars
rather than being fetched — see §5.

---

## 1. Detections

### Where they come from

```
blah2 (C++)
  └─ TCP → blah2-api :3002        (network.ports.detection)
       ├─ enriches with ADS-B association
       ├─ serves latest value at GET /api/detection
       └─ forwardToTracker() → retina-tracker :30100   (when enabled)
```

blah2 emits one JSON object per CPI over a raw TCP socket. `blah2-api` accumulates
bytes until the payload ends in `}`, parses, enriches, and stores it in a module-level
variable. There is **no history** — `/api/detection` is a latest-value register.

### Wire shape out of blah2

Built by `Detection::to_json` in `blah2-arm/src/data/Detection.cpp:45`:

```json
{
  "timestamp": 1753900000123,
  "delay":   [12.4, 30.1],
  "doppler": [-118.0, 44.5],
  "snr":     [14.2, 9.8]
}
```

| Field | Unit as sent | Notes |
|---|---|---|
| `timestamp` | **epoch milliseconds**, integer | `time[0]/1000` where `time[0]` is `current_time_us()` |
| `delay` | **kilometres** (bistatic range) | `delay_bin_to_km` applied at `blah2.cpp:315` before the socket write |
| `doppler` | Hz | from the ambiguity map's Doppler axis |
| `snr` | dB | `10*log10(|x|) - noisePower`, `CfarDetector1D.cpp:48` |

All three arrays are quantised to **2 decimal places** — `writer.SetMaxDecimalPlaces(2)`
at `Detection.cpp:76`. For `delay` that is 0.01 km ≈ 10 m ≈ 0.033 µs. Do not emit more
precision than this upstream; it would be fictional.

### Array length is guaranteed equal

`to_json` builds all three arrays from a single loop bounded by `get_nDetections()`,
which returns `delay.size()`. Every producer stage pushes the three vectors in lockstep:

- `src/process/detection/CfarDetector1D.cpp:89-90`
- `src/process/detection/Centroid.cpp:65-67`
- `src/process/detection/Interpolate.cpp:84-86`

**Caveat:** nothing validates this. `Detection`'s constructor accepts three vectors of
any lengths, and `get_nDetections()` trusts `delay.size()`, so a future desync would
read out of bounds silently rather than throw. We assert equal length in
`collect/blah2.py` regardless — a malformed frame must never reach the server, and
checking at the read point keeps it out of the slot entirely.

An empty frame serialises as three empty arrays. That is a normal, meaningful state
(detector running, nothing detected) and the spec wants it sent.

### ADS-B association is added by blah2-api, not blah2

`blah2-arm/api/server.js:328-366`. Only when `truth.adsb.enabled` is true. It adds a
parallel `adsb` array to the object:

```json
"adsb": [ { "hex": "4ca1f2", "lat": …, "lon": …, "alt": …,
            "expected_delay": …, "expected_doppler": …,
            "delay_residual": …, "doppler_residual": … }, null ]
```

Three consequences for us:

1. When ADS-B is disabled there is **no `adsb` key at all** — synthesise `[null] * n`.
2. Entries are objects, not hex strings. The spec's `adsb_hex` wants `.hex`.
3. It is a **tolerance-gated single-best match**, not truth. Both tolerances are
   per-node config (`truth.adsb.delay_tolerance: 2.0`, `doppler_tolerance: 5.0`), so
   association strictness varies node to node. See open question Q7.

### Rate

`process.data.cpi: 0.5` → **2 Hz**, matching the spec's "2 Hz today".

### Recommended tap: poll `GET /api/detection`

blah2-api runs on `network_mode: host`, port 3000 (`network.ports.api`).

Polling a latest-value register is normally the wrong shape, but the spec's transport
is explicitly *latest-wins with at most one request in flight*. The API's semantics and
the transport's semantics match exactly, so polling is correct here and costs **zero
changes to blah2 or blah2-api**.

- Poll at ~4–5 Hz against a 2 Hz producer to reduce aliasing misses.
- Dedupe on `timestamp` — a repeat means we polled twice within one CPI.
- Missed frames are acceptable and expected. That is the design.

The lossless alternative is generalising `forwardToTracker` (`api/server.js`) into a
fan-out to N sinks and having the agent be sink #2. Keep that in the back pocket for
v2 if the miss rate turns out to matter; it needs a blah2 release to land.

Do **not** take retina-tracker's TCP ingest port. It accepts one connection at a time
and retina-gui already owns it (see the comment block at the top of
`retina-gui/src/retina_tracker_client.py`).

---

## 2. Timestamp semantics

`time[0] = current_time_us()` is taken at `blah2.cpp:244`, *after* the ring buffer
already holds a full CPI, and immediately before the oldest CPI is popped off the
front. So the samples in a frame were captured over roughly `[t-cpi, t]`:

**`t` is the end of the capture window, not the start and not the middle.**

Resolution is one CPI (0.5 s) and that is the practical floor — one timestamp per
frame is all blah2 produces. Against the server's 4 s association window, ±0.25 s of
quantisation is ~6% and not worth chasing.

What *is* worth fixing is that the window edge is undocumented, and that the offset
scales with `cpi`, which is per-node config the server does not currently receive.
Two nodes on different `cpi` have different common-mode offsets and the server cannot
correct for it. See open question Q3 — the fix is one field, `cpi_s`, in `NodeConfig`.

### Capture gaps

`IqData::push_back` (`src/data/IqData.cpp:42`) is a ring that **silently evicts the
oldest sample** when full — no back-pressure, no counter. Buffer capacity is
`tCpi * tBuffer * fs` (`blah2.cpp:103`) = 0.75 s at the current `cpi: 0.5`,
`buffer: 1.5`, `fs: 2e6`. Note `buffer` is a *multiplier on CPI*, not seconds.

With 0.25 s of headroom, a node whose CPI processing time exceeds 0.5 s starts losing
samples immediately. Each extracted CPI is still internally contiguous (the consumer
holds the lock for the whole extraction), so measurements within a frame are valid.
What is lost is temporal *coverage* between frames — a target crossing during the gap
never appears.

This is detectable without any new mechanism: `t[n+1] - t[n] > cpi_s` means capture
was dropped in between. Another reason `cpi_s` earns its place in `NodeConfig`.

Note the distinction, because the spec conflates them under `seq`:

| Signal | Reveals |
|---|---|
| `seq` gaps | **transport** loss — deliberate and constant under latest-wins |
| `t` spacing > `cpi_s` | **capture** loss — invisible any other way |

---

## 3. Node identity

Script: `owl-os/configuration/mender/identity/mender-device-identity` (Jinja-templated
by the Ansible build). Resolution order:

1. If `/data/mender/node_id` exists and is non-empty → echo `node_id=<cached>`, exit.
2. If `/proc/cpuinfo` matches `Raspberry Pi|BCM` and `Serial` is present and not all
   zeros → `ret` + last 8 hex of serial, persisted atomically, echo `node_id=…`.
3. Otherwise → echo **`mac=<address>`** from `/sys/class/net/{end0|eth0}/address`
   (`end0` on pi4-v3/pi5-v3, `eth0` elsewhere). Exits 1 if that file is missing too.

**The MAC fallback exists but cannot masquerade as a node.** It emits the attribute key
`mac=`, not `node_id=`, and never writes `/data/mender/node_id`. So the spec's
`^ret[0-9a-f]{8}$` pattern holds, and a hard local error on a missing file is both
correct and achievable. Path 3 only fires on non-Pi hardware in practice.

### `board_model` comes from the same directory

`RegisterRequest.board_model` is required, node-reported and diagnostic only. Take it
from **`/data/mender/device_type`**, which sits beside `node_id` — so it costs no new
interface, no new mount, and the same read-once-at-boot lifecycle.

On the Owl node (2026-08-06) it contains:

```
device_type=pi5-v3-arm64
```

Note the format differs from its neighbour: `device_type` is `key=value`, `node_id` is
bare. Strip the prefix.

Why this rather than `/proc/device-tree/model`, which gives
`"Raspberry Pi 5 Model B Rev 1.1"`:

- Mender targets artifacts **by device type**, so this string decides which software the
  board is allowed to receive. "Which build stream is this node on" is a question
  someone will actually ask; "what board revision is it" is not.
- The spec's own example, `"raspberrypi5-4gb"`, is a device-type slug rather than a
  hardware description — so this matches the shape the author had in mind, even though
  the value differs. See Q15.
- It is stable. The device-tree string carries a board revision that changes without
  meaning anything to us.

Unlike `node_id`, a missing `board_model` is **not** fatal — it is diagnostic only, so
losing it must never stop a node registering. The two readers differ in failure
behaviour on purpose.

### Two landmines

1. `retina-gui/src/app.py:96` — `get_node_id()` returns the **string `'Unknown'`** when
   the file is missing. It is display-only there. Do not import or imitate it: this
   agent needs its own reader that raises. `"Unknown"` must never reach a registration
   payload.
2. `retina-node/config/default.yml` has `network.node_id: "ret000000000"` — a static
   placeholder, identical on every node, 12 characters, and it fails the spec's
   pattern. Nothing here should read it. Worth deleting upstream.

---

## 4. Configuration

Source of truth on the node: `/data/retina-node/config/config.yml`, produced by
`config-merger` from `defaults` + `user.yml` at stack start. Mount it read-only.

### Mapping to the spec's `NodeConfig`

| Spec field | Node source | Conversion |
|---|---|---|
| `rx_lat` | `location.rx.latitude` | — |
| `rx_lon` | `location.rx.longitude` | — |
| `rx_alt_ft` | `location.rx.altitude` | **metres → feet**, × 3.28084 |
| `tx_lat` | `location.tx.latitude` | — |
| `tx_lon` | `location.tx.longitude` | — |
| `tx_alt_ft` | `location.tx.altitude` | **metres → feet**, × 3.28084 |
| `tx_callsign` | `location.tx.name` | free-text display name, not a regulatory callsign — Q5 |
| `fc_hz` | `capture.fc` | — |
| `fs_hz` | `capture.fs` | — |
| `max_range_km` | `process.ambiguity.delayMax` | `delayMax × c / fs / 1000` = 60 km at 400 bins / 2 MHz — Q6 |
| `beam_width_deg` | **does not exist** | Q1 — blocking |
| `beam_azimuth_deg` | **does not exist** | Q1 — blocking |

`beam_width_deg` and `beam_azimuth_deg` returned zero hits across owl-os, retina-node,
retina-gui and blah2-arm. These are new config fields plus GUI plumbing, not a mapping.

### Agreement, and the telemetry opt-in

`RegisterRequest.agreement` requires `{version, accepted_at}`. Today:

- The setup wizard has an `agreements` step (`retina-gui/src/device_state.py:382`).
- `templates/eula.html` is explicitly *"Placeholder - to be replaced with actual terms."*
- `templates/setup/_agreements.html` has a checkbox that only enables the Continue
  button. **Nothing persists an acceptance record anywhere.**

So registration cannot be populated today. Blocking — Q2.

**Decided:** telemetry is opt-in, via an explicit action in the setup wizard. That
record and the agreement record are the same artifact — opt-in state plus
`{version, accepted_at}`, written by retina-gui, read by us. One file, not two.

It lives at **`/data/retina-gui/telemetry-consent.json`**, alongside retina-gui's other
device state. `DATA_DIR` is `/data/retina-gui` in production and `<repo>/dev_data` under
`DEV_MODE` (`retina-gui/src/services.py:49-51`), and `DeviceState` hangs `install.lock`,
`setup-wizard.json`, `mender-update.status` and the rest off it
(`retina-gui/src/device_state.py:41-53`).

```json
{
  "opted_in": true,
  "agreement": { "version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z" }
}
```

Two things not to inherit from the neighbouring `cloud-services-disabled`: it is an
**empty file with negative sense**, which cannot carry `{version, accepted_at}`, and
`not exists` is indistinguishable from "the wizard never ran". Follow
`setup-wizard.json` instead — JSON with content, positive sense. The nesting is
deliberate: `agreement` maps straight onto the spec's `Agreement` schema so stage 2
lifts the object wholesale, and `opted_in` stays separate because accepting the terms
and consenting to telemetry are different facts.

A missing file means "not opted in". That is a normal state, so `consent.py` is
complete and testable before retina-gui writes anything.

An opted-out node still runs this container: it reads the flag, idles without
registering, and keeps its status document fresh. Removing it from compose instead
would make an opt-out require a stack restart, and would lose the ability to say why
nothing is being sent.

### "Cloud services" in retina-gui is the Mender toggle, not this

Easy to misread. `device_state.is_cloud_services_enabled()`
(`retina-gui/src/device_state.py:123-125`) is `not exists(<data>/cloud-services-disabled)`,
and `set_cloud_services` backs up and restores `mender.conf`
(`retina-gui/src/services.py:101`). It governs Mender OTA and nothing else. There is no
existing signal anywhere in the stack that means "the user wants telemetry on" — hence
the new record above.

It is still worth **reading**, because the dependency runs the other way: the spec
requires Mender enrolment and acceptance before registration can succeed. Cloud services
disabled means no Mender, which means registration sits in the opaque `403`
indefinitely. Reading the flag is the difference between "stuck in 403, cause unknown"
and "stuck in 403 because the operator turned Mender off."

### `board_model`

Not a config field. It comes from `/data/mender/device_type` — see §3, where it sits
beside `node_id`.

---

## 5. Health inputs

Health needs **no interface beyond the four in §0.** An earlier draft of this document
had container state coming from the docker socket and `versions` from `docker inspect`;
neither is necessary, and dropping the socket removes a mount from the one container on
the node that talks to the internet.

| Field | Source |
|---|---|
| `cpu_pct`, `temp_c`, `disk_free_mb` | host `/proc`, `/sys/class/thermal`, `statvfs` |
| `blah2` | derived from the detection poll we already run — see below |
| `adsb` | `truth.adsb.enabled` from config, plus whether the `adsb` key appears on polled frames |
| `queue_depth` | meaningless under latest-wins — Q11 |
| `versions.*` | compose env vars, injected into our container |
| `state` | ours to define — see below |

### blah2 liveness is better derived than inspected

The spec's own `NodeHealth` description concedes the weakness: `blah2: "up"` and
`queue_depth: 0` "read identically on a wedged node and a working one." Container state
from the docker socket has exactly that problem — a wedged blah2 has a perfectly healthy
container.

The detection poll answers it for free. A blah2 that is up but wedged returns a
`timestamp` that stops advancing, which we see because we are already polling at ~4 Hz
and deduping on that field. Three distinguishable states, no new interface:

| Observation | Means |
|---|---|
| poll fails / connection refused | blah2-api down |
| poll succeeds, `timestamp` advancing | working |
| poll succeeds, `timestamp` stale | wedged — the case the socket cannot see |

Same trick for ADS-B: `truth.adsb.enabled` says whether it is meant to be on, and the
presence of the `adsb` key on returned frames says whether it is actually working. That
distinguishes "off by configuration" from "enabled but broken" without reaching for
tar1090 or adsb2dd directly.

### versions come from compose, not from docker

`retina-node/docker-compose.yml` already pins every image through an env var with a
default — `BLAH2_V`, `TAR1090_V`, `ADSB2DD_V`, `CONFIG_MERGER_V`, `RETINA_TRACKER_V`.
Passing the relevant ones into this container as environment is a compose change of a
few lines and needs no privileged access to anything.

### `state` has a vocabulary clash

`HeartbeatRequest.state` is required. The spec's example value is `"streaming"`.
retina-gui's `device_state.get_state()` (`retina-gui/src/device_state.py:57-64`) returns
`'idle'`, `'updating_gui'` or `'updating_server'`.

Those are two different axes — one is "am I sending data", the other is "is an update in
progress" — so this is not a mapping. The `updating_*` values are worth folding in, since
a node that goes quiet mid-update is explained by them, but the vocabulary should be
ours. See `docs/implementation-plan.md`, "Still to settle".

### Available and deliberately not collected

The spec is the scope. Anything it does not ask for is not gathered, however easy it
would be — otherwise the payload accretes fields nobody agreed to and the node ends up
with mounts and dependencies it cannot justify.

| Available | Why it is not collected |
|---|---|
| `GET /api/timing` | The `cpi` total over `cpi × 1000` ms is the only view of ring-buffer loss (§2, §5b), but there is no field for it. Would be a spec proposal, not a collection change |
| `/capture/overload-status`, `/capture/rf-status` | Not in the spec. Relevant to the RF overload incident on Owl, but that is a local diagnosis problem |
| Pi throttle flags | No field. `vcgencmd get_throttled` works but needs `/dev/vcio` mounted plus the Pi userland binary in the image; `/sys/class/hwmon/*/name == rpi_volt` exposes `in0_lcrit_alarm` for free, but with nowhere to send it that is moot |
| `truth.adsb.delay_tolerance`, `doppler_tolerance` | Q7 proposes sending them so the server knows what it is comparing. Until it does, nothing local needs them |

If any of these become genuinely necessary, the route is an open question to the server
author, not a field we invent.

---

## 5b. What actually runs on a node

Verified on the Owl node, 2026-08-06. Worth having written down because two reasonable
assumptions about it are wrong.

| Layer | What |
|---|---|
| systemd, host | `mender-authd`, `mender-connect`, `mender-updated`, `retina-gui.service`, `retina-node.service` (oneshot) |
| compose project `retina-node` | blah2, blah2-api, blah2-web, blah2-host, tar1090, adsb2dd, retina-tracker |

**retina-gui is not a container.** It is a systemd unit running
`/opt/retina-gui/src/app.py` directly on the host. That is how it escapes the
project-wide `--force-recreate` it issues when applying configuration — by being outside
Docker, not by being a second compose project. There is exactly one compose project on
the node.

**Observed CPI behaviour is processing-bound, and this is expected.** Configured
`cpi: 0.5`, but frames arrive every **886 ms** (lifetime average over two days;
p50 886, p90 907, max 1047 over a 90 s sample). `/api/timing` reports 927 ms of
processing per CPI, 602 ms of which is `clutter_filter`. So the node emits at ~1.13 Hz
rather than the spec's assumed 2 Hz, and since processing exceeds the 0.75 s ring buffer
(`cpi × buffer`), capture is being dropped continuously.

Two consequences for us:

1. **A staleness window must derive from the observed frame period, not `cpi_s`.** The
   configured value is not what the node honours, and is out by 1.8× here.
2. It makes Q3 sharper. The server sees 1.13 Hz and cannot tell a configured rate from a
   node dropping half its capture coverage, because it never receives `cpi_s`.

Expanding the ring buffer is the fix and is separate work — there is RAM headroom. This
is recorded as expected behaviour, not a defect.

---

## 6. Container conventions

From `retina-node/docker-compose.yml`, which this service must slot into:

- Image `ghcr.io/offworldlabs/retina-telemetry:${TELEMETRY_V:-v0.1.0}`, tag pinned by
  an env var with a default, same as every other service.
- Config mounted read-only from `${CONFIG_DIR:-/data/retina-node/config}`.
- `restart: always`.
- **No `depends_on` gated on blah2 health.** The most valuable telemetry moment is
  when blah2 is crash-looping; a container that waits for blah2 to be healthy is dead
  exactly when it is most needed.
- `network_mode: host` so `127.0.0.1:3000` reaches blah2-api. Bind no listening ports.
- Token lives under `/data` so it survives an OS update — mount a path there
  read-write, mode 0600 on the file.
- **No docker socket.** Nothing needs it: liveness is derived from the detection poll
  and versions come from env (§5).
- Pass the image-tag env vars already used for pinning — `BLAH2_V`, `TAR1090_V`,
  `ADSB2DD_V` and friends — into this container so it can report `versions`.

## 7. Reference implementations worth borrowing

- `retina-gui/src/retina_tracker_client.py` — JSONL tailer that already handles the
  truncate-on-restart case, and a best-effort TCP sender with lazy reconnect. If track
  events are ever added (Q8), start here.
- `retina-gui/src/config_manager.py` — how the GUI reads and writes node config.
- `retina-gui/src/device_state.py` — install locks and Mender status, for the
  heartbeat `state` field.
