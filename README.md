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

**Status:** built, implementing spec `1.1.1`. Verified end to end on a live node against
a tunnelled mock — every endpoint, every reachable state, and the refusal paths.

One thing blocks a real deployment, and it is not in this repo: **nothing on a node
persists the three consent records**, so no node can register. That is retina-gui's
work. See `CLAUDE.md`.

## Documents

- [`docs/node-ingest-v1.yml`](docs/node-ingest-v1.yml) — the server contract (read-only input)
- [`docs/data-sources.md`](docs/data-sources.md) — where node data comes from, in what units, with what gotchas
- [`CLAUDE.md`](CLAUDE.md) — architecture, conventions, and the decisions worth not re-litigating

## How it's built

Staged by layer rather than by endpoint, so that the retry, auth and response-handling
machinery shared by all four endpoints gets written once instead of four times.

| Stage | Owns | Knows about |
|---|---|---|
| **1 — Collection** | the interfaces to the rest of the node stack | the node only |
| **2 — Construction** | turning what we collected into wire payloads | both sides |
| **3 — Communication** | request machinery, lifecycle, the two traffic disciplines | the server only |

Stage 3 knows nothing about radar; stage 1 knows nothing about the server. Stages 1 and
2 need no server and no mock, which is how they were built while registration was still
blocked upstream.

The payload models in `wire/models.py` are **generated** from the spec
(`tools/generate-models.sh`), so the server's own constraints do the validating and a
revision upstream shows exactly which fields moved.

## Checks

```
tools/check.sh                 # every gate CI runs
tools/check.sh --tracked       # the same, against a clean copy of tracked files
```

`--tracked` matters: gitignored scratch files are invisible to CI but present locally,
so the working tree can pass a gate CI will fail. It has caught that twice.

## Testing against a real node

```
tools/live-probe.sh owl        # stages 1 and 2, read-only
tools/live-service.sh owl      # the whole service against a tunnelled mock
tools/live-failures.sh owl     # the server's refusals: 409, 429, pause, 401
tools/live-stress.sh owl       # restarts, and a node with an unreadable config
tools/live-stalled.sh owl      # stops blah2 to reach `stalled` — writes to the node
```

Each ships the package over ssh and runs it in a stock container with the node's paths
mounted read-only. Nothing is built and nothing persists. Watch any of them live at
`http://127.0.0.1:18080/`, served by the mock itself.

`live-stalled.sh` is the only one that writes to the node: it stops the four blah2
containers and restarts them from an EXIT trap over a separate ssh connection, so a dead
tunnel cannot strand the radar.

These have found bugs unit tests structurally could not — Docker's data-root sitting on
`/data`, a required-but-nullable field dropped during serialisation, and a heartbeat that
silently never sent because a payload builder returned a model instead of a dict.

## Design in one paragraph

Detections are **latest-wins**: at most one request in flight, a newer frame replaces a
pending one rather than queueing behind it, and dropped frames are correct behaviour —
the server's association window is 4 s, so a late frame is worse than a missing one.
Registration, config and heartbeat are the opposite and retry until they land. The
service must never share fate with the stack it reports on: it carries no health-gated
dependency on blah2, because the moment blah2 is crash-looping is the moment its
telemetry matters most.
