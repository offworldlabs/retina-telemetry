# retina-telemetry

Node-side telemetry uplink for the RETINA passive radar fleet.

One container per node. It owns everything the node sends to the server — registration,
detection streaming, heartbeat and configuration sync — so that no other service on the
node needs its own retry, auth or backoff logic.

```
blah2 ──▶ blah2-api ──▶ retina-telemetry ──HTTPS──▶ api.retina.fm
                 │           ▲    │
config.yml ──────┤           │    └──▶ status.json ──▶ retina-gui
node_id ─────────┤           │
opt-in record ───┘           │
host metrics ────────────────┘
```

Nothing in the stack pushes to it — there is no event bus and it binds no ports. Every
input is a poll or a file read, and the status document is how a failure reaches the
operator.

**Status:** stages 1 and 2 built — collection and construction, 177 tests, both verified
against a live node with `tools/live-probe.sh`. Stage 3 (communication) is next and does
not exist yet. Two questions with the server team still block registration; everything
else is unblocked.

## Documents

- [`docs/node-ingest-v1.yml`](docs/node-ingest-v1.yml) — the server contract (read-only input)
- [`docs/data-sources.md`](docs/data-sources.md) — where node data comes from, in what units, with what gotchas
- [`docs/open-questions.md`](docs/open-questions.md) — outstanding asks for the server team
- [`docs/implementation-plan.md`](docs/implementation-plan.md) — architecture, module layout, build stages

## How it's built

Staged by layer rather than by endpoint, so that the retry, auth and response-handling
machinery shared by all four endpoints gets written once instead of four times.

| Stage | Owns | Knows about | |
|---|---|---|---|
| **1 — Collection** | the interfaces to the rest of the node stack | the node only | built |
| **2 — Construction** | turning what we collected into wire payloads | both sides | built |
| **3 — Communication** | `3a` machinery, `3b` lifecycle, `3c` the two disciplines | the server only | — |

Stage 3 knows nothing about radar; stage 1 knows nothing about the server. Stages 1 and
2 are testable with no server and no mock — which is how they were built, since the two
blocking questions are upstream of us.

The payload models in `wire/models.py` are **generated** from the spec
(`tools/generate-models.sh`), so the server's own constraints do the validating and a
revision upstream shows exactly which fields moved.

## Testing against a real node

```
tools/live-probe.sh owl        # both stages
tools/live-probe.sh owl 2      # wire only
```

Ships the package over ssh and runs it in a stock container with the node's paths
mounted read-only. Nothing is built, nothing is pushed to a registry, nothing persists.
Both stages have found bugs this way that unit tests structurally could not — stage 1's
probe revealed that Docker's data-root sits on `/data`, and stage 2's revealed a
required-but-nullable field being dropped during serialisation.

## Design in one paragraph

Detections are **latest-wins**: at most one request in flight, a newer frame replaces a
pending one rather than queueing behind it, and dropped frames are correct behaviour —
the server's association window is 4 s, so a late frame is worse than a missing one.
Registration, config and heartbeat are the opposite and retry until they land. The
service must never share fate with the stack it reports on: it carries no health-gated
dependency on blah2, because the moment blah2 is crash-looping is the moment its
telemetry matters most.
