"""A local stand-in for `api.retina.fm`, for developing and testing stage 3.

Run it, point the service at it:

    python tools/mock_server.py --port 8080
    curl -s localhost:8080/v1/nodes/register -d '{...}'

Or start it inside a test, on a random port, with no process management:

    with MockServer() as server:
        ...                       # server.url is http://127.0.0.1:<port>/v1
        assert server.requests[-1].path == "/v1/nodes/detection"

One implementation, two uses. Standard library only — four endpoints do not
justify a web framework, and this must not add a runtime dependency.

## Why not Prism

Generating a mock from the OpenAPI document gives correctly-shaped replies for
free, but it always cooperates. Everything worth testing in stage 3 is about
the server *refusing*: a revoked token must stop the stream without triggering
re-registration, a `409` must force a config resend and then resume, a
`Retry-After` must actually be waited out. None of that is reachable from a
server that only says yes, so the control channel below is the point of this
file rather than an extra.

## Fidelity: it copies the server's code, not only the spec

This used to validate against the generated models and answer the spec's
declared status codes. That made it a model of the *contract*, and the contract
is not what a node meets. Tower-Finder's implementation departs from it in five
places, all of them reachable by a healthy node, and a mock that answered the
spec would have let every one of them reach a real deployment unrehearsed:

1. **Schema failures are `422`, not `400`**, in FastAPI's `{"detail": [...]}`
   shape, where `detail` is a *list*. The spec declares no 422 anywhere. This
   is what a node meets for a bad `node_id`, an unknown key, or mismatched
   parallel arrays.
2. **`401` has two shapes.** `PUT /nodes/config` catches its own dependency and
   answers `{"error": "unauthorized"}`; detection and heartbeat let FastAPI
   render `{"detail": "unauthorized"}`. Same condition, two bodies.
3. **`409` is narrower than "the version does not match".** A version the
   server *issued and has since superseded* is accepted with `config_stale`,
   because `node_configs` is append-only and the frame is still interpretable.
   Only a version it never issued is a 409. The old mock 409'd on any mismatch,
   which is stricter than production and never generated the ack a node really
   meets in the seconds after it PUTs a new configuration.
4. **`413` is `{"error": "too_large"}`**, not the spec's `payload_too_large`.
5. **Registration is refused with `403`, never `429`**, by a limiter of 5 an
   hour and 20 a day per `node_id`, spending its allowance before it knows
   whether the device is real.

So the four handlers below are transcriptions of `routes/node_register.py`,
`routes/node_config.py` and `routes/node_stream.py`, and `validate_config` and
the two rate limiters are transcriptions of the services they call. Where the
server and the spec disagree, **this file follows the server**, and says so at
the point it does. Its job is to be the thing a node actually talks to.

It is still deliberately *not* a simulator. Mender is a knob rather than a
lookup, and the limiters' key-exhaustion bounds are omitted because a node
cannot observe them.
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pydantic

from retina_telemetry.wire.models import (
    DetectionFrame,
    HeartbeatRequest,
    RegisterRequest,
)

BASE_PATH = "/v1"

#: The spec's origin-side body caps, in bytes: "Request bodies are size-capped
#: at the origin, ahead of parsing: 8 KiB for registration, heartbeat and
#: configuration, 64 KiB for a detection frame."
#:
#: Enforced here because schema validation alone let a 10 KiB heartbeat through
#: that production would have refused unread — and the node carrying twenty
#: distinct long faults is precisely the one whose beat matters most.
BODY_CAPS = {
    "register": 8 * 1024,
    "heartbeat": 8 * 1024,
    "config": 8 * 1024,
    "detection": 64 * 1024,
}
MAX_BODY_BYTES = 64 * 1024


class _RegisterEnvelope(RegisterRequest):
    """``RegisterRequest`` with ``config`` left untyped, as the server has it.

    The server's model declares ``config: dict[str, Any]`` deliberately: a typed
    model would 422 on a bad configuration value *before* the handler runs,
    putting a config-shaped rejection in front of identity resolution and making
    the difference between it and a 403 an oracle for which node identities
    exist. Validation is ``validate_config`` below, called once the identity has
    resolved.

    Our generated ``RegisterRequest`` types it as ``NodeConfig``, which is right
    for the client — it should not be able to *build* an invalid one. Overriding
    it here is what lets the mock reproduce the server's two-stage refusal.
    """

    config: dict[str, Any]


#: Endpoint name → (method, path, request model). ``None`` for config: the
#: server reads that body by hand rather than declaring it on the signature,
#: so that a body-shaped refusal cannot precede the 401.
ENDPOINTS: dict[str, tuple[str, str, type[pydantic.BaseModel] | None]] = {
    "register": ("POST", f"{BASE_PATH}/nodes/register", _RegisterEnvelope),
    "detection": ("POST", f"{BASE_PATH}/nodes/detection", DetectionFrame),
    "heartbeat": ("POST", f"{BASE_PATH}/nodes/heartbeat", HeartbeatRequest),
    "config": ("PUT", f"{BASE_PATH}/nodes/config", None),
}


# ── the refusal taxonomy, from services/node_refusals.py ──────────────

#: Unknown device, not yet accepted by Mender, Mender unreachable, a lost
#: registration race and rate limiting are one byte-identical body. A difference
#: in wording or key order is as much of an oracle as a difference in code.
REFUSAL_BODY = {"error": "forbidden"}
RATE_LIMITED_BODY = {"error": "rate_limited"}

#: Unrelated to any real window, and jittered, so that the delay says nothing
#: about which refusal class produced it.
RETRY_AFTER_BASE_S = 300
RETRY_AFTER_JITTER_S = 60


def refusal_retry_after() -> int:
    return RETRY_AFTER_BASE_S + secrets.randbelow(2 * RETRY_AFTER_JITTER_S) - RETRY_AFTER_JITTER_S


# ── the rate limiters, from services/node_rate_limits.py ──────────────

#: (requests admitted, window in seconds), per node_id. Both must have room
#: before either is spent. The server counts *every admitted attempt*, including
#: one that turns out to name an identity Mender has not accepted, because
#: crediting it back after the lookup is the same as not limiting the lookup.
#:
#: The consequence is worth rehearsing rather than reading: a node waiting on
#: Mender acceptance spends its allowance while it waits, and our client honours
#: the 300 s ``Retry-After`` over its own backoff, so it exhausts the daily 20 in
#: about four hours and is then refused for the rest of the day whatever Mender
#: says. That is the divergence this limiter exists here to make reachable.
REGISTRATION_LIMITS: tuple[tuple[int, int], ...] = ((5, 3600), (20, 86400))

#: endpoint → (requests admitted, window in seconds), per (node_id, endpoint).
#: The server's table also carries ``config: (30, 60)``, but its config handler
#: does not call the limiter, so neither does this. Sized against the contract's
#: 2 Hz ceiling rather than Owl's measured 0.6–0.9 Hz.
ENDPOINT_LIMITS: dict[str, tuple[int, int]] = {
    "detection": (8, 1),
    "heartbeat": (30, 60),
}


@dataclass(frozen=True)
class Refusal:
    """What a limiter decided. Rendering it is the handler's business."""

    status: int
    body: dict[str, str]
    retry_after_s: int


class _FixedWindowCounters:
    """Counts admitted requests per key per window.

    Windows are aligned on the clock rather than sliding from first use —
    ``(floor(now / window) + 1) * window`` — which is what makes the wait the
    server reports a countdown to a real boundary. A burst straddling one gets a
    fresh allowance part way through, and reproducing that is the point: it is
    the reason the server's own tests freeze the clock.

    The server's key-exhaustion bound is omitted. It caps how many distinct
    identities an unauthenticated caller can manufacture, which is a property of
    the server's memory rather than anything a node can observe.
    """

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, object], tuple[float, int]] = {}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()

    def admit(self, specs: list[tuple[tuple[str, object], int, int]]) -> float | None:
        """Admit against every spec, or return the wait in seconds.

        Every spec must have room before any is incremented: a request refused
        by the daily limit must not consume the hourly one.
        """
        now = self._clock()
        with self._lock:
            for key, limit, _window_s in specs:
                window_end, count = self._counters.get(key, (0.0, 0))
                if window_end > now and count >= limit:
                    return window_end - now
            for key, _limit, window_s in specs:
                window_end, count = self._counters.get(key, (0.0, 0))
                if window_end <= now:
                    window_end, count = float((math.floor(now / window_s) + 1) * window_s), 0
                self._counters[key] = (window_end, count + 1)
            return None


class RegistrationRateLimiter:
    """Keyed on the ``node_id`` in the body, which is the only identity an
    unauthenticated caller offers. Refuses with the shared 403 rather than a
    429: a 429 would tell a caller its identity is known enough to be counted."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._counters = _FixedWindowCounters(clock)

    def reset(self) -> None:
        self._counters.reset()

    def admit(self, node_id: str) -> Refusal | None:
        specs = [
            ((node_id, i), limit, window) for i, (limit, window) in enumerate(REGISTRATION_LIMITS)
        ]
        if self._counters.admit(specs) is None:
            return None
        return Refusal(403, dict(REFUSAL_BODY), refusal_retry_after())


class TokenRateLimiter:
    """Per (node_id, endpoint), so a node streaming at its ceiling can still be
    heard from. The heartbeat is the one thing a node in trouble has left."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._counters = _FixedWindowCounters(clock)

    def reset(self) -> None:
        self._counters.reset()

    def admit(self, node_id: str, endpoint: str) -> Refusal | None:
        if endpoint not in ENDPOINT_LIMITS:
            return None
        limit, window_s = ENDPOINT_LIMITS[endpoint]
        wait_s = self._counters.admit([((node_id, endpoint), limit, window_s)])
        if wait_s is None:
            return None
        return Refusal(429, dict(RATE_LIMITED_BODY), max(1, math.ceil(wait_s)))


# ── the configuration validator, from services/node_config.py ─────────

#: About 0.11 m. Below this the receiver and illuminator are the same point as
#: far as the solver is concerned, whatever the node believes it measured.
_MIN_BASELINE_DEG = 1e-6


class ConfigInvalid(Exception):
    def __init__(self, field_name: str, reason: str = "out of range") -> None:
        super().__init__(f"{field_name}: {reason}")
        self.field = field_name
        self.reason = reason


#: field -> (low, high, low_inclusive, high_inclusive), in the server's own
#: order, which is **not** alphabetical. It decides which field a payload wrong
#: in several places names back, so the order is part of the behaviour.
#: Both beam fields are absent because both are nullable and handled separately.
_NUMERIC_BOUNDS: dict[str, tuple[float, float, bool, bool]] = {
    "rx_lat": (-90, 90, True, True),
    "rx_lon": (-180, 180, True, True),
    "rx_alt_ft": (-1500, 30000, True, True),
    "tx_lat": (-90, 90, True, True),
    "tx_lon": (-180, 180, True, True),
    "tx_alt_ft": (-1500, 30000, True, True),
    "fc_hz": (1_000_000, 6_000_000_000, True, True),
    "fs_hz": (100_000, 20_000_000, True, True),
    "max_range_km": (0, 1000, False, True),
    "cpi_s": (0, 10, False, True),
    # The contract sets no maximum on either tolerance, so the bound is inf
    # rather than a ceiling of the server's own.
    "delay_tolerance_us": (0, math.inf, False, True),
    "doppler_tolerance_hz": (0, math.inf, False, True),
}

_REQUIRED = set(_NUMERIC_BOUNDS) | {"tx_callsign", "beam_width_deg", "beam_azimuth_deg"}

#: The fifteen fields a configuration version consists of, which is exactly
#: ``validate_config``'s output. Two versions are the same version when these
#: fifteen agree.
CONFIG_FIELDS = tuple(_REQUIRED)


def _number(field_name: str, value: Any) -> float:
    """Three shapes a plain range check would let through.

    ``bool`` subclasses ``int``, so ``true`` would be filed as a latitude of 1.
    A huge integer literal has no float representation. And NaN compares false
    against every bound, so it survives a range check untouched.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigInvalid(field_name, "not a number")
    try:
        number = float(value)
    except OverflowError:
        raise ConfigInvalid(field_name, "out of range") from None
    if not math.isfinite(number):
        raise ConfigInvalid(field_name, "not a finite number")
    return number


def validate_config(payload: Any) -> dict[str, Any]:
    """Return the normalised configuration, or raise naming exactly one field.

    Three of these checks are not in the spec and cannot be expressed in a JSON
    schema, so a client that validates only against the generated models can
    still be refused here: a degenerate baseline, a bool where a number belongs,
    and NaN.

    Which field a wrong-in-several-places payload names is deliberate and worth
    reproducing: unknown and missing keys are reported **alphabetically first**,
    while a bounds violation is reported in ``_NUMERIC_BOUNDS`` order. So two
    fields out of range name the earlier one in the validator's order rather
    than the earlier one in the alphabet, and a node retrying unchanged always
    gets the same answer.
    """
    # A JSON body is not necessarily an object, and every check below assumes it
    # is. "config" is the key registration nests this under, and the whole body
    # on PUT.
    if not isinstance(payload, dict):
        raise ConfigInvalid("config", "not an object")

    unknown = sorted(set(payload) - _REQUIRED)
    if unknown:
        raise ConfigInvalid(unknown[0], "unknown field")

    missing = sorted(_REQUIRED - set(payload))
    if missing:
        raise ConfigInvalid(missing[0], "missing")

    out: dict[str, Any] = {}
    for field_name, (low, high, low_inclusive, high_inclusive) in _NUMERIC_BOUNDS.items():
        value = _number(field_name, payload[field_name])
        below = value < low if low_inclusive else value <= low
        above = value > high if high_inclusive else value >= high
        if below or above:
            raise ConfigInvalid(field_name)
        out[field_name] = value

    callsign = payload["tx_callsign"]
    if not isinstance(callsign, str) or not 1 <= len(callsign) <= 32:
        raise ConfigInvalid("tx_callsign")
    out["tx_callsign"] = callsign

    # Required and nullable since 1.1.1, and null is what the whole fleet sends.
    # The interval is (0, 360], which is not the azimuth's below.
    width = payload["beam_width_deg"]
    if width is None:
        out["beam_width_deg"] = None
    else:
        width = _number("beam_width_deg", width)
        if not 0 < width <= 360:
            raise ConfigInvalid("beam_width_deg")
        out["beam_width_deg"] = width

    # null is meaningful: broadside or omnidirectional. Coercing it to 0.0 would
    # silently aim every unaimed node in the fleet due north.
    azimuth = payload["beam_azimuth_deg"]
    if azimuth is None:
        out["beam_azimuth_deg"] = None
    else:
        azimuth = _number("beam_azimuth_deg", azimuth)
        if not 0 <= azimuth < 360:
            raise ConfigInvalid("beam_azimuth_deg")
        out["beam_azimuth_deg"] = azimuth

    if (
        abs(out["rx_lat"] - out["tx_lat"]) < _MIN_BASELINE_DEG
        and abs(out["rx_lon"] - out["tx_lon"]) < _MIN_BASELINE_DEG
    ):
        raise ConfigInvalid("tx_lat", "receiver and illuminator are at the same point")

    return out


#: The contract's bound on ``Error.detail``. An unknown field is named back to
#: the caller and a field name is caller-supplied JSON, so 513 characters of it
#: — well inside the 8 KiB body cap — would otherwise fail the server's own
#: response model and turn a 400 into a 500 the node then retries forever.
ERROR_DETAIL_MAX = 512


#: A live view of what the node is posting, at http://127.0.0.1:<port>/.
#:
#: Exists because a request log printed after a run answers "what happened"
#: while a node under test raises "what is happening" — a paused stream, a
#: revoked token, a radar that has stopped are all things you want to watch
#: arrive rather than reconstruct afterwards. Polls its own control channel, so
#: it works against a run already in progress and needs nothing served from
#: anywhere else.
LIVE_PAGE = """<!doctype html><meta charset="utf-8"><title>retina ingest mock</title>
<style>
 :root{--bg:#0d1117;--fg:#e6edf3;--dim:#7d8590;--line:#21262d;--ok:#3fb950;
       --warn:#d29922;--bad:#f85149;--acc:#58a6ff}
 body{background:var(--bg);color:var(--fg);font:13px ui-monospace,SFMono-Regular,Menlo,monospace;
      margin:0;padding:1rem 1.25rem}
 h1{font-size:.8rem;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
    margin:0 0 .75rem;font-weight:600}
 .counts{display:flex;gap:1.5rem;margin-bottom:1rem;flex-wrap:wrap}
 .c b{color:var(--acc);font-size:1.5rem;font-weight:600}
 .c span{color:var(--dim);margin-left:.35rem}
 table{border-collapse:collapse;width:100%}
 th{text-align:left;color:var(--dim);font-weight:600;font-size:.7rem;letter-spacing:.1em;
    text-transform:uppercase;padding:.3rem .6rem;border-bottom:1px solid var(--line)}
 td{padding:.28rem .6rem;border-bottom:1px solid var(--line);vertical-align:top;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 td.body{white-space:pre;color:var(--dim);max-width:none}
 .detection{color:var(--acc)} .heartbeat{color:var(--ok)}
 .config{color:var(--warn)} .register{color:var(--bad)}
 .state{font-weight:600}
 .paused,.stalled{color:var(--warn)} .error{color:var(--bad)} .streaming{color:var(--ok)}
 #status{color:var(--dim);margin-bottom:.75rem}
</style>
<h1>retina ingest mock &mdash; live</h1>
<div id="status">connecting…</div>
<div class="counts" id="counts"></div>
<table><thead><tr><th>at</th><th>endpoint</th><th>summary</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
const EP = ["register","config","heartbeat","detection"];
function summarise(r){
  const b = r.body || {};
  if (r.endpoint === "detection")
    return `seq ${b.seq} boot ${String(b.boot_id||"").slice(0,8)} cv ${b.config_version} `
         + `n=${(b.delay||[]).length}`;
  if (r.endpoint === "heartbeat"){
    const h = b.health || {};
    const errs = (b.errors||[]).length;
    return `<span class="state ${b.state}">${b.state}</span> cv ${b.config_version===null?"null":b.config_version}`
         + ` cpu ${h.cpu_pct===null?"null":h.cpu_pct} temp ${h.temp_c===null?"null":h.temp_c}`
         + ` blah2 ${h.blah2===null?"null":h.blah2}${h.adsb?" adsb "+h.adsb:""}`
         + (errs?` <span class="error">errors ${errs}</span>`:"");
  }
  if (r.endpoint === "config")
    return `beam ${b.beam_width_deg===null?"null":b.beam_width_deg}/`
         + `${b.beam_azimuth_deg===null?"null":b.beam_azimuth_deg} cpi ${b.cpi_s}`;
  if (r.endpoint === "register") return `${b.node_id} ${b.board_model}`;
  return "";
}
async function tick(){
  try{
    const r = await fetch("/_control/requests");
    const all = (await r.json()).requests;
    const counts = {}; EP.forEach(e => counts[e] = 0);
    all.forEach(x => { if (x.endpoint in counts) counts[x.endpoint]++; });
    document.getElementById("counts").innerHTML = EP.map(e =>
      `<div class="c"><b class="${e}">${counts[e]}</b><span>${e}</span></div>`).join("");
    document.getElementById("rows").innerHTML = all.slice(-200).reverse().map(x =>
      `<tr><td>${(x.at||"").slice(11,23)}</td>`
      + `<td class="${x.endpoint}">${x.endpoint||x.path}</td>`
      + `<td class="body">${summarise(x)}</td></tr>`).join("");
    document.getElementById("status").textContent =
      `${all.length} requests · updated ${new Date().toLocaleTimeString()}`;
  }catch(e){ document.getElementById("status").textContent = "mock unreachable"; }
}
tick(); setInterval(tick, 1000);
</script>
"""

#: A body that was not JSON at all. Distinct from a body that was the JSON
#: literal ``null``, which the config handler and FastAPI treat differently.
MALFORMED = object()


# ── the three guards a JSON Schema cannot express ─────────────────────
#
# The server hand-writes these onto its request models, so they do not appear in
# the OpenAPI document and our generated models cannot carry them. Without them
# the mock is *laxer* than production on the hot path, which is the wrong
# direction for a mock to err in: a frame the mock accepts and the server 422s
# is a frame silently dropped on a real node.

#: Numeric leaves per endpoint, as field → whether it is a list. The server's
#: ``Number`` and ``Count`` aliases put a ``BeforeValidator`` on each of these
#: rejecting a bool or a string, and ``Number`` also sets
#: ``allow_inf_nan=False``. ``bool`` matters because it subclasses ``int``, so
#: ``"snr": [true]`` would otherwise be filed as 1.0; the string case matters
#: because pydantic's lax mode coerces ``"14.2"`` happily.
_NUMERIC_LEAVES: dict[str, dict[str, bool]] = {
    "detection": {
        "t": False,
        "seq": False,
        "config_version": False,
        "delay": True,
        "doppler": True,
        "snr": True,
    },
    "heartbeat": {"uptime_s": False, "config_version": False},
}

#: The same guard one level down, since ``NodeHealth`` is built from the same
#: aliases.
_HEALTH_LEAVES = ("cpu_pct", "disk_free_mb", "temp_c")


@dataclass(frozen=True)
class _GuardFailure:
    """One entry of a 422 ``detail`` list, built by hand."""

    loc: list[Any]
    msg: str
    type: str = "value_error"
    input: Any = None


def _check_number(value: Any, loc: list[Any]) -> _GuardFailure | None:
    if value is None:
        return None
    if isinstance(value, bool | str):
        return _GuardFailure(
            loc, "Value error, expected a number, not a boolean or a string", input=value
        )
    if isinstance(value, float) and not math.isfinite(value):
        return _GuardFailure(loc, "Input should be a finite number", "finite_number", value)
    return None


def _guard_failures(endpoint: str, body: Any) -> list[_GuardFailure]:
    """The server's hand-written guards, run ahead of schema validation.

    Ahead, because they are ``BeforeValidator``s on the server: a string where a
    number belongs is refused as a string rather than coerced and then
    bounds-checked.
    """
    if not isinstance(body, dict):
        return []
    failures: list[_GuardFailure] = []

    for name, is_list in _NUMERIC_LEAVES.get(endpoint, {}).items():
        value = body.get(name)
        if is_list and isinstance(value, list):
            failures += [
                f for i, v in enumerate(value) if (f := _check_number(v, ["body", name, i]))
            ]
        elif not is_list:
            if failure := _check_number(value, ["body", name]):
                failures.append(failure)

    if endpoint == "heartbeat" and isinstance(body.get("health"), dict):
        for name in _HEALTH_LEAVES:
            if failure := _check_number(body["health"].get(name), ["body", "health", name]):
                failures.append(failure)

    if endpoint == "detection":
        arrays = ("delay", "doppler", "snr", "adsb_hex")
        lengths = {len(body[a]) for a in arrays if isinstance(body.get(a), list)}
        if len(lengths) > 1:
            # The four arrays are one table on the server's side, so a mismatch
            # means the frame does not say what it appears to. A model-level
            # validator there, so the location is the body rather than a field.
            failures.append(
                _GuardFailure(
                    ["body"],
                    "Value error, delay, doppler, snr and adsb_hex must be the same length",
                )
            )

    return failures


@dataclass
class RecordedRequest:
    """What the server saw, for a test to assert against."""

    method: str
    path: str
    endpoint: str | None
    body: Any
    authorization: str | None
    at: datetime

    @property
    def bearer(self) -> str | None:
        if self.authorization and self.authorization.startswith("Bearer "):
            return self.authorization.removeprefix("Bearer ")
        return None


@dataclass
class ScriptedResponse:
    """A reply queued by the control channel, consumed once."""

    status: int
    body: dict[str, Any] | None = None
    retry_after: int | None = None


@dataclass
class NodeRecord:
    """One row of ``nodes``, plus the configuration versions it has been issued.

    ``configs`` is append-only, which is what makes the 409 rule work: a frame
    stamped with a superseded version is still interpretable, because the
    geometry it was computed under is still readable. Only a version that was
    never issued is uninterpretable.
    """

    node_id: str
    node_ref: str
    board_model: str
    #: "active" or "blocked". A block survives re-registration deliberately —
    #: a reflash must not be the way to undo an operator's decision.
    status: str = "active"
    active_config_version: int | None = None
    configs: dict[int, dict[str, Any]] = field(default_factory=dict)

    def upsert_config(self, config: dict[str, Any]) -> int:
        """Return the active version, minting one only if the values differ.

        The comparison is field by field in Python and deliberately not a
        database predicate: ``NULL = NULL`` is never true in SQL, and
        ``beam_width_deg`` is null on every node in the fleet. Comparing in the
        database would mint a version on every resend for the whole fleet, tell
        each node ``config_stale`` in perpetuity, and have each one resend in
        answer. The server's own plan calls this the single most likely server
        bug, so it is worth having the mock get it right.
        """
        active = self.configs.get(self.active_config_version or 0)
        # ``.get`` rather than ``[]``: a version forced through the control
        # channel holds no configuration at all, and a sparse record should
        # compare unequal — as a version the node has never sent should — rather
        # than raise.
        if active is not None and all(active.get(name) == config[name] for name in CONFIG_FIELDS):
            return self.active_config_version  # type: ignore[return-value]
        # Counted from the highest version ever held rather than from the active
        # one, so a superseded row can never have its number reused.
        version = (max(self.configs) if self.configs else 0) + 1
        self.configs[version] = config
        self.active_config_version = version
        return version


@dataclass
class State:
    """Everything the mock knows. Guarded by :attr:`lock`."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: list[RecordedRequest] = field(default_factory=list)
    #: TCP connections accepted, not requests served. The client is meant to
    #: hold one open across every request, so this is how keep-alive is proved.
    connections: int = 0
    scripted: dict[str, list[ScriptedResponse]] = field(default_factory=dict)

    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    #: Live bearer tokens, token → node_id. A revoked token is removed, which is
    #: how a re-registration kills its predecessor.
    tokens: dict[str, str] = field(default_factory=dict)

    #: ^(nde|sim)[0-9a-z]{12}$ — fifteen characters. The old fixture was
    #: sixteen and would not have matched, which is the sort of thing a
    #: mock quietly gets away with until the real server does not.
    node_ref: str = "nde4f2k9xq7m3b8"

    #: What the Mender lookup would answer: "accepted", "pending", "unknown" or
    #: "down". Scripted rather than modelled, because all four are the same
    #: opaque 403 to a node and only the operator can tell them apart. It is a
    #: knob because it is the ordinary reason a real node cannot register.
    mender: str = "accepted"

    registration_limiter: RegistrationRateLimiter = field(default_factory=RegistrationRateLimiter)
    token_limiter: TokenRateLimiter = field(default_factory=TokenRateLimiter)

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.connections = 0
            self.scripted.clear()
            self.nodes.clear()
            self.tokens.clear()
            self.mender = "accepted"
        self.registration_limiter.reset()
        self.token_limiter.reset()

    def only_node(self) -> NodeRecord | None:
        """The one node, for the control channel and the state view.

        The mock is talked to by one node. The records are keyed by ``node_id``
        anyway, because the registration limiter is keyed on it and the whole
        point of that key is that one board in a retry loop must not lock the
        rest of the fleet out.
        """
        return next(iter(self.nodes.values()), None)


def _now() -> str:
    """RFC 3339 UTC with a ``Z``, which is the form every example in the spec
    uses and the one the node parses to measure its clock offset. Pydantic's
    default renders ``+00:00``; the server overrides it for this reason."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 so keep-alive works: the client is meant to hold one connection
    # open across every request, and a mock that closed each time would hide
    # any bug in that.
    protocol_version = "HTTP/1.1"

    state: State  # injected by MockServer

    def setup(self) -> None:
        # Called once per accepted connection rather than per request.
        super().setup()
        with self.state.lock:
            self.state.connections += 1

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ── plumbing ─────────────────────────────────────────────────────

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return MALFORMED

    def _send(
        self, status: int, body: dict[str, Any] | None = None, retry_after: int | None = None
    ) -> None:
        # default=str mirrors FastAPI's jsonable_encoder, which is what lets a
        # 422 carry the offending `input` however exotic it was.
        encoded = json.dumps(body or {}, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(encoded)

    def _taxonomy(
        self, status: int, error: str, detail: str | None = None, retry_after: int | None = None
    ) -> None:
        """The contract's ``Error``. ``detail`` is dropped when absent, never
        serialised as ``"detail": null`` — the contract types it as a string
        with no null member, and the server's ErrorBody enforces that."""
        body: dict[str, Any] = {"error": error}
        if detail is not None:
            body["detail"] = detail[:ERROR_DETAIL_MAX]
        self._send(status, body, retry_after)

    def _unprocessable(self, failures: pydantic.ValidationError | list[_GuardFailure]) -> None:
        """FastAPI's 422, which is **not** in the contract at all.

        ``detail`` is a *list* of error objects here, where every other refusal
        this server emits puts a string under ``error``. A node parsing only the
        taxonomy meets this shape on every malformed request, so the mock has to
        produce it rather than a tidy 400.
        """
        if isinstance(failures, pydantic.ValidationError):
            detail = [
                {
                    # FastAPI prefixes the location with the parameter the body
                    # arrived as.
                    "type": error["type"],
                    "loc": ["body", *error["loc"]],
                    "msg": error["msg"],
                    "input": error.get("input"),
                }
                for error in failures.errors(include_url=False)
            ]
        else:
            detail = [
                {"type": f.type, "loc": f.loc, "msg": f.msg, "input": f.input} for f in failures
            ]
        self._send(422, {"detail": detail})

    def _validated(self, endpoint: str, body: Any) -> Any:
        """Run the server's hand-written guards, then the schema. ``None`` means
        a 422 has already been sent."""
        if body is MALFORMED:
            body = None
        if guards := _guard_failures(endpoint, body):
            self._unprocessable(guards)
            return None
        model = ENDPOINTS[endpoint][2]
        assert model is not None
        try:
            return model.model_validate(body)
        except pydantic.ValidationError as exc:
            self._unprocessable(exc)
            return None

    def _refuse(self) -> None:
        """Unknown device, not yet accepted, and Mender unreachable, identically."""
        self._send(403, dict(REFUSAL_BODY), refusal_retry_after())

    def _refusal(self, refusal: Refusal) -> None:
        self._send(refusal.status, refusal.body, refusal.retry_after_s)

    def _endpoint_for(self, method: str, path: str) -> str | None:
        for name, (m, p, _) in ENDPOINTS.items():
            if m == method and p == path:
                return name
        return None

    # ── dispatch ─────────────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if self.path == "/_control/requests":
            with self.state.lock:
                self._send(
                    200,
                    {
                        "requests": [
                            {
                                "method": r.method,
                                "path": r.path,
                                "endpoint": r.endpoint,
                                "bearer": r.bearer,
                                "body": r.body if r.body is not MALFORMED else None,
                                "at": r.at.isoformat(),
                            }
                            for r in self.state.requests
                        ]
                    },
                )
            return
        if self.path == "/_control/state":
            with self.state.lock:
                node = self.state.only_node()
                self._send(
                    200,
                    {
                        "token_issued": bool(self.state.tokens),
                        "config_version": node.active_config_version if node else None,
                        "streaming_allowed": node.status == "active" if node else True,
                        "node_status": node.status if node else None,
                        "node_ref": node.node_ref if node else self.state.node_ref,
                        "mender": self.state.mender,
                        "connections": self.state.connections,
                        "scripted": {k: len(v) for k, v in self.state.scripted.items()},
                    },
                )
            return
        if self.path in ("/", "/_control/live"):
            page = LIVE_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        self._taxonomy(404, "not_found")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/_control/"):
            self._control()
            return
        self._handle("POST")

    # ── control channel ──────────────────────────────────────────────

    def _control(self) -> None:
        body = self._read_body()
        if body is MALFORMED or body is None:
            body = {}

        if self.path == "/_control/reset":
            self.state.reset()
            self._send(200, {"ok": True})
            return

        if self.path == "/_control/enqueue":
            endpoint = body.get("endpoint")
            if endpoint not in ENDPOINTS:
                self._taxonomy(400, "unknown_endpoint", f"expected one of {sorted(ENDPOINTS)}")
                return
            queued = ScriptedResponse(
                status=int(body["status"]),
                body=body.get("body"),
                retry_after=body.get("retry_after"),
            )
            with self.state.lock:
                self.state.scripted.setdefault(endpoint, []).extend(
                    [queued] * int(body.get("count", 1))
                )
            self._send(200, {"ok": True})
            return

        if self.path == "/_control/levels":
            with self.state.lock:
                if "mender" in body:
                    self.state.mender = str(body["mender"])
                if "node_ref" in body:
                    self.state.node_ref = str(body["node_ref"])
                node = self.state.only_node()
                if node is not None:
                    # streaming_allowed is not a level the server holds. It
                    # derives it from node.status on every response, so that is
                    # what the knob sets — a blocked node, which is the only
                    # thing that makes it false.
                    if "streaming_allowed" in body:
                        node.status = "active" if body["streaming_allowed"] else "blocked"
                    if "node_ref" in body:
                        node.node_ref = str(body["node_ref"])
                    # Likewise config_stale: the server compares the version the
                    # node reported against the active one. Moving the active
                    # version is how staleness actually arises, and it is the
                    # only way to make the ack say so.
                    if "config_version" in body:
                        version = int(body["config_version"])
                        # Recorded as issued, so that a frame still carrying it
                        # is stale rather than a 409. It holds no configuration,
                        # so the node's next PUT mints the version after it —
                        # which is what a real divergence looks like.
                        node.configs.setdefault(version, {})
                        node.active_config_version = version
            self._send(200, {"ok": True})
            return

        self._taxonomy(404, "not_found")

    # ── the four endpoints ───────────────────────────────────────────

    def _handle(self, method: str) -> None:
        endpoint = self._endpoint_for(method, self.path)

        # Enforced before parsing, the way the spec says the origin does it, and
        # answered in the node error taxonomy the way the server's middleware
        # does: `{"error": "too_large"}`, with no detail. The contract declares
        # no 413 on any endpoint, so the body is the server's choice rather than
        # anything transcribed.
        declared = int(self.headers.get("Content-Length") or 0)
        cap = BODY_CAPS.get(endpoint or "", MAX_BODY_BYTES)
        if declared > cap:
            self._taxonomy(413, "too_large")
            return

        body = self._read_body()

        with self.state.lock:
            self.state.requests.append(
                RecordedRequest(
                    method=method,
                    path=self.path,
                    endpoint=endpoint,
                    body=body,
                    authorization=self.headers.get("Authorization"),
                    at=datetime.now(UTC),
                )
            )
            scripted = self.state.scripted.get(endpoint or "", [])
            queued = scripted.pop(0) if scripted else None

        if endpoint is None:
            self._taxonomy(404, "not_found")
            return

        # A scripted reply short-circuits everything, including auth and the
        # limiters — the point is to reproduce what the server does, not to
        # justify it.
        if queued is not None:
            self._send(queued.status, queued.body, queued.retry_after)
            return

        getattr(self, f"_{endpoint}")(body)

    def _bearer_node(self, *, taxonomy: bool) -> NodeRecord | None:
        """Resolve the Authorization header to a node, or answer 401.

        ``taxonomy`` picks which of the server's **two** 401 bodies to send.
        ``PUT /nodes/config`` catches its dependency's HTTPException and
        restores the contract's ``{"error": "unauthorized"}``; detection and
        heartbeat take the same dependency through ``Depends`` and let FastAPI
        render its own ``{"detail": "unauthorized"}``. Same condition, two
        shapes, depending only on which path met it.
        """
        header = self.headers.get("Authorization") or ""
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else None
        with self.state.lock:
            node_id = self.state.tokens.get(token) if token else None
            node = self.state.nodes.get(node_id) if node_id else None
        if node is None:
            if taxonomy:
                self._taxonomy(401, "unauthorized")
            else:
                self._send(401, {"detail": "unauthorized"})
            return None
        return node

    def _register(self, body: Any) -> None:
        # FastAPI validates the request model before the handler runs, so this
        # 422 precedes the rate limiter and the Mender lookup both. `config` is
        # untyped here, so nothing about its *contents* is refused yet.
        request = self._validated("register", body)
        if request is None:
            return

        # Before the Mender call, so an unauthenticated caller cannot drive
        # traffic at the Mender tenant by looping on registration. Every
        # admitted attempt is counted, including one that turns out to name an
        # identity Mender has not accepted.
        refusal = self.state.registration_limiter.admit(request.node_id)
        if refusal is not None:
            self._refusal(refusal)
            return

        if self.state.mender != "accepted":
            self._refuse()
            return

        # Identity has resolved, so a 400 is now safe to return: reachable any
        # earlier and the difference between 400 and 403 would be an oracle for
        # which node identities exist.
        try:
            config = validate_config(request.config)
        except ConfigInvalid as exc:
            # `detail` is the field name rather than a sentence: retina-gui puts
            # it next to the input it belongs to and owns the wording there.
            self._taxonomy(400, "invalid_config", exc.field)
            return

        with self.state.lock:
            node = self.state.nodes.get(request.node_id)
            if node is None:
                node = NodeRecord(
                    node_id=request.node_id,
                    node_ref=self.state.node_ref,
                    board_model=request.board_model,
                )
                self.state.nodes[request.node_id] = node
            else:
                # Re-registration is allowed rather than gated on an operator
                # reactivation, which would permanently brick any reflashed
                # board. node.status is deliberately left alone: a board an
                # operator blocked comes back blocked.
                node.board_model = request.board_model
                for token, owner in list(self.state.tokens.items()):
                    if owner == request.node_id:
                        del self.state.tokens[token]
            # The version the server holds, which is 1 only for a node it has
            # not seen. Telling a returning board 1 when the server holds 4
            # gives it a 409 on every frame afterwards.
            version = node.upsert_config(config)
            token = f"tok_{secrets.token_urlsafe(24)}"
            self.state.tokens[token] = request.node_id
            node_ref = node.node_ref

        self._send(
            200,
            {
                "token": token,
                "node_ref": node_ref,
                "config_version": version,
                "server_time": _now(),
            },
        )

    def _detection(self, body: Any) -> None:
        # Dependencies are solved before the body is validated, so a bad token
        # beats a malformed frame: 401 wins over 422.
        node = self._bearer_node(taxonomy=False)
        if node is None:
            return
        frame = self._validated("detection", body)
        if frame is None:
            return

        refusal = self.state.token_limiter.admit(node.node_id, "detection")
        if refusal is not None:
            self._refusal(refusal)
            return

        with self.state.lock:
            stale = frame.config_version != node.active_config_version
            # A superseded version is a *known* version, and that is the
            # distinction the contract draws. Answering 409 to every mismatch
            # would make config_stale on the ack unreachable, and would refuse
            # the frames already in flight when a node PUTs a new configuration
            # — a re-PUT loop rather than a recovery.
            if stale and frame.config_version not in node.configs:
                self._taxonomy(409, "unknown_config_version")
                return
            # A blocked node is told to pause rather than refused: a refusal is
            # indistinguishable from a fault, and a node that believes itself
            # faulty retries. Its frames stay out of the pipeline meanwhile,
            # which is what the block is for.
            allowed = node.status == "active"
            self._send(
                202,
                {
                    "accepted": len(frame.delay) if allowed else 0,
                    "config_stale": stale,
                    "streaming_allowed": allowed,
                },
            )

    def _heartbeat(self, body: Any) -> None:
        node = self._bearer_node(taxonomy=False)
        if node is None:
            return
        beat = self._validated("heartbeat", body)
        if beat is None:
            return

        refusal = self.state.token_limiter.admit(node.node_id, "heartbeat")
        if refusal is not None:
            self._refusal(refusal)
            return

        with self.state.lock:
            self._send(
                200,
                {
                    "server_time": _now(),
                    # `null` is a version too: a node that holds none has nothing
                    # the server issued, so a server that holds one has something
                    # for it to fetch.
                    "config_stale": beat.config_version != node.active_config_version,
                    "streaming_allowed": node.status == "active",
                    "node_ref": node.node_ref,
                },
            )

    def _config(self, body: Any) -> None:
        # The bearer is resolved before the body is touched, which is why the
        # server reads this body by hand rather than declaring it on the
        # signature: a body-shaped refusal ahead of identity resolution would
        # make the difference between 400 and 401 an oracle for live tokens.
        node = self._bearer_node(taxonomy=True)
        if node is None:
            return

        if body is MALFORMED:
            # A body that is not JSON at all fails the same way a body that is
            # JSON but not a configuration does, and the remedy is the same:
            # resending it unchanged will not help. Not a 500, which the node
            # would retry.
            self._taxonomy(400, "invalid_config", "config")
            return

        try:
            config = validate_config(body)
        except ConfigInvalid as exc:
            self._taxonomy(400, "invalid_config", exc.field)
            return

        # No rate limit. The server's table carries one for this endpoint and
        # its handler does not call it; the mock matches the handler.
        with self.state.lock:
            version = node.upsert_config(config)
        self._send(200, {"config_version": version})


class MockServer:
    """The mock, as a context manager. Binds port 0 unless told otherwise."""

    def __init__(self, port: int = 0, verbose: bool = False) -> None:
        self.state = State()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._httpd.verbose = verbose  # type: ignore[attr-defined]
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        """Base URL including the version prefix, as the spec's server block has it."""
        return f"http://127.0.0.1:{self.port}{BASE_PATH}"

    @property
    def connections(self) -> int:
        """TCP connections accepted. One, if the client keeps its alive."""
        with self.state.lock:
            return self.state.connections

    @property
    def requests(self) -> list[RecordedRequest]:
        with self.state.lock:
            return list(self.state.requests)

    @property
    def node(self) -> NodeRecord | None:
        """The registered node, for a test that wants to read its state.

        Takes :attr:`State.lock` itself, so **do not** call it from inside a
        ``with server.state.lock`` block: the lock is a plain ``threading.Lock``
        and re-entering it deadlocks the caller against a live handler thread.
        Use :meth:`block` and :meth:`move_config_version` to mutate, which is
        what a test wants anyway.
        """
        with self.state.lock:
            return self.state.only_node()

    def block(self, blocked: bool = True) -> None:
        """Set the node's status, which is what ``streaming_allowed`` derives from."""
        with self.state.lock:
            if node := self.state.only_node():
                node.status = "blocked" if blocked else "active"

    def move_config_version(self, version: int) -> None:
        """Move the server's active version out from under a streaming node.

        The version is recorded as issued but holds no configuration, so frames
        still carrying the old one are *stale* rather than unknown — the 202
        path, not the 409 — and the node's resend mints the version after this
        one. That is what a real divergence looks like from the node's side.
        """
        with self.state.lock:
            if node := self.state.only_node():
                node.configs.setdefault(version, {})
                node.active_config_version = version

    def received(self, endpoint: str) -> list[RecordedRequest]:
        return [r for r in self.requests if r.endpoint == endpoint]

    def enqueue(
        self,
        endpoint: str,
        status: int,
        *,
        body: dict[str, Any] | None = None,
        retry_after: int | None = None,
        count: int = 1,
    ) -> None:
        """Queue a scripted reply, consumed once per request."""
        if endpoint not in ENDPOINTS:
            raise ValueError(f"unknown endpoint {endpoint!r}, expected one of {sorted(ENDPOINTS)}")
        with self.state.lock:
            self.state.scripted.setdefault(endpoint, []).extend(
                [ScriptedResponse(status, body, retry_after)] * count
            )

    def start(self) -> MockServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=2)

    def __enter__(self) -> MockServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--quiet", action="store_true", help="suppress the request log")
    args = parser.parse_args()

    server = MockServer(port=args.port, verbose=not args.quiet)
    print(f"mock ingest on {server.url}")
    print("  control:  POST /_control/reset | /_control/enqueue | /_control/levels")
    print("            GET  /_control/requests | /_control/state")
    try:
        server.start()
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    main()
