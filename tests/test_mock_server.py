"""The mock is test infrastructure, so it needs testing itself.

A mock that silently accepts a malformed payload would let a real bug through
stage 3 unnoticed, which is worse than having no mock at all.
"""

import dataclasses
import json
import urllib.error
import urllib.request

import pytest

from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.serialise import to_wire
from tests.wire.test_config import OWL
from tools.mock_server import MockServer


@pytest.fixture
def server():
    with MockServer() as running:
        yield running


class Clock:
    """A hand-wound clock, for the tests that assert on a rate limit.

    The limiters' windows are aligned on the clock, so a burst straddling a
    boundary is handed a fresh allowance part way through — which makes "was
    the ninth refused?" depend on when the burst happened to start. Left on
    ``time.monotonic`` that is a coin weighted by how loaded the runner is: it
    came up tails once on CI, against a merge that had touched none of this.

    Starts mid-window rather than on a boundary so that nothing depends on the
    two coinciding, and advances only when a test says so.
    """

    def __init__(self, now: float = 1000.5) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def frozen_server(clock):
    """A mock whose limiter windows move only when :class:`Clock` is wound."""
    with MockServer(clock=clock) as running:
        yield running


def post(url, body=None, token=None, method="POST"):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else b"{}",
        method=method,
        headers={"Content-Type": "application/json"}
        | ({"Authorization": f"Bearer {token}"} if token else {}),
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc), dict(exc.headers)


def _registration(**overrides):
    return {
        "node_id": "ret824685c9",
        "board_model": "pi5-v3-arm64",
        "agreements": {
            "licence": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
            "remote_management": {
                "version": "2026-07-01",
                "accepted_at": "2026-07-31T09:12:00Z",
            },
            "publication": {
                "version": "2026-07-01",
                "accepted_at": "2026-07-31T09:12:00Z",
                "choice": "public",
            },
        },
        "config": to_wire(build_node_config(OWL)),
    } | overrides


def register(server):
    status, body, _ = post(f"{server.url}/nodes/register", _registration())
    assert status == 200
    return body["token"], body["config_version"]


#: Hand-built rather than produced by the builders, on purpose: these tests
#: check the *mock*, so a payload the builders and the mock both got wrong
#: would pass. Kept in step with the spec by hand.
BOOT_ID = "28a156bd3f8652f4"


def frame(config_version=1, seq=1):
    return {
        "t": 1786014064.679,
        "seq": seq,
        "boot_id": BOOT_ID,
        "config_version": config_version,
        "delay": [41.362],
        "doppler": [-118.0],
        "snr": [14.2],
        "adsb_hex": [None],
    }


def beat(config_version=1):
    return {
        "state": "streaming",
        "uptime_s": 181569,
        "config_version": config_version,
        "boot_id": BOOT_ID,
    }


# ── the happy path ───────────────────────────────────────────────────


def test_registration_mints_a_token(server):
    token, version = register(server)

    assert token.startswith("tok_")
    assert version >= 1


def test_detection_is_accepted_and_counted(server):
    token, version = register(server)

    status, body, _ = post(f"{server.url}/nodes/detection", frame(version), token)

    assert status == 202
    assert body["accepted"] == 1
    assert body["streaming_allowed"] is True


def test_heartbeat_restates_the_levels(server):
    token, version = register(server)

    status, body, _ = post(f"{server.url}/nodes/heartbeat", beat(version), token)

    assert status == 200
    assert set(body) == {"server_time", "config_stale", "streaming_allowed", "node_ref"}


def test_empty_frame_is_accepted(server):
    """A detector running with nothing detected — 41 of 101 frames on Owl."""
    token, version = register(server)
    empty = frame(version) | {"delay": [], "doppler": [], "snr": [], "adsb_hex": []}

    status, body, _ = post(f"{server.url}/nodes/detection", empty, token)

    assert status == 202
    assert body["accepted"] == 0


# ── auth ─────────────────────────────────────────────────────────────


def test_streaming_without_a_token_is_refused(server):
    status, _, _ = post(f"{server.url}/nodes/detection", frame())

    assert status == 401


def test_a_wrong_token_is_refused(server):
    register(server)

    status, _, _ = post(f"{server.url}/nodes/detection", frame(), "tok_wrong")

    assert status == 401


def test_registration_needs_no_token(server):
    """Its security block is empty in the spec — it is what mints the token."""
    token, _ = register(server)

    assert token


# ── it validates like the real server ────────────────────────────────


def test_a_malformed_node_id_is_rejected(server):
    """`node_id` carries the spec's pattern on the request model, so the server
    refuses it before the handler runs — which is also what keeps an empty one
    away from the Mender lookup, where it would match the first record carrying
    no identity at all."""
    status, body, _ = post(f"{server.url}/nodes/register", _registration(node_id="Unknown"))

    # 422, not the spec's 400: FastAPI renders its own shape for a model it
    # rejected, and nothing on the server converts it.
    assert status == 422
    assert [e["loc"] for e in body["detail"]] == [["body", "node_id"]]
    assert "pattern" in body["detail"][0]["msg"].lower()


def test_a_schema_failure_is_422_in_fastapis_shape_not_the_taxonomy(server):
    """The divergence a node meets on every malformed request.

    `detail` is a *list* here, where every refusal the contract declares puts a
    string under `error`. Nothing on the server converts it, so a client that
    parses only the taxonomy has to survive this.
    """
    token, version = register(server)
    broken = frame(version)
    del broken["adsb_hex"]

    status, body, _ = post(f"{server.url}/nodes/detection", broken, token)

    assert status == 422
    assert "error" not in body
    assert isinstance(body["detail"], list)
    assert ["body", "adsb_hex"] in [e["loc"] for e in body["detail"]]


def test_mismatched_parallel_arrays_are_422(server):
    """The four arrays are one table on the server's side, so a short one means
    the frame does not say what it appears to."""
    token, version = register(server)
    lopsided = frame(version) | {"snr": [14.2, 9.8]}

    status, _, _ = post(f"{server.url}/nodes/detection", lopsided, token)

    assert status == 422


def test_a_frame_naming_a_node_is_refused(server):
    """Attribution comes from the token, so a body that names a node is a
    refusal rather than a question about which of the two to believe."""
    token, version = register(server)

    status, _, _ = post(
        f"{server.url}/nodes/detection", frame(version) | {"node_ref": "nde000000000000"}, token
    )

    assert status == 422


def test_a_config_with_null_beam_fields_is_accepted(server):
    """An uncharacterised antenna, which is every node in the fleet. The mock
    validates against the same generated schema the server will use, so this is
    the closest thing we have to proof the shape is right."""
    token, _ = register(server)
    bare = to_wire(build_node_config(dataclasses.replace(OWL, beam_width_deg=None)))
    assert bare["beam_width_deg"] is None

    status, _, _ = post(f"{server.url}/nodes/config", bare, token, method="PUT")

    assert status == 200


def test_dropping_a_required_null_is_rejected(server):
    """The reason wire/serialise.py exists, demonstrated end to end rather than
    argued. `exclude_none=True` drops beam_azimuth_deg, which the spec requires,
    and the server refuses the payload."""
    token, _ = register(server)
    naive = build_node_config(OWL).model_dump(mode="json", exclude_none=True)
    assert "beam_azimuth_deg" not in naive

    status, _, _ = post(f"{server.url}/nodes/config", naive, token, method="PUT")

    assert status == 400


def test_the_correctly_serialised_config_is_accepted(server):
    token, _ = register(server)
    status, body, _ = post(
        f"{server.url}/nodes/config",
        to_wire(build_node_config(OWL)),
        token,
        method="PUT",
    )

    assert status == 200
    assert body["config_version"] >= 1


# ── the control channel ──────────────────────────────────────────────


def test_a_scripted_401_is_served_once(server):
    token, version = register(server)
    server.enqueue("detection", 401)

    first, _, _ = post(f"{server.url}/nodes/detection", frame(version), token)
    second, _, _ = post(f"{server.url}/nodes/detection", frame(version), token)

    assert (first, second) == (401, 202)


def test_a_scripted_429_carries_retry_after(server):
    """Stage 3 must honour it rather than backing off on its own schedule."""
    token, version = register(server)
    server.enqueue("detection", 429, retry_after=30)

    status, _, headers = post(f"{server.url}/nodes/detection", frame(version), token)

    assert status == 429
    assert headers["Retry-After"] == "30"


def test_a_scripted_403_can_repeat(server):
    """Registration sits in 403 until an operator opens a reflash window, so
    the retry discipline has to survive a run of them."""
    server.enqueue("register", 403, retry_after=60, count=3)

    for _ in range(3):
        status, _, headers = post(f"{server.url}/nodes/register", {})
        assert status == 403
        assert headers["Retry-After"] == "60"

    assert register(server)[0]


def test_levels_can_be_flipped_mid_run(server):
    """streaming_allowed is restated on every response, not sent on an edge, so
    a node that missed one still learns.

    The server holds no such flag: it derives the level from the node's status
    on each response, and a blocked node is the only thing that makes it false.
    """
    token, version = register(server)

    server.block()

    _, body, _ = post(f"{server.url}/nodes/detection", frame(version), token)

    assert body["streaming_allowed"] is False
    assert body["accepted"] == 0  # telling it to pause is the courtesy; this is the block


def test_a_version_the_server_never_issued_is_409(server):
    token, _ = register(server)

    status, body, _ = post(f"{server.url}/nodes/detection", frame(config_version=99), token)

    assert status == 409
    assert body == {"error": "unknown_config_version"}


def test_a_superseded_version_is_accepted_and_reported_stale(server):
    """The narrower 409, and the case a node meets every time it PUTs.

    A version the server issued and has since replaced is not an unknown one:
    the configuration table is append-only precisely so the geometry a frame was
    computed under stays readable, which is what makes the frame interpretable.
    409-ing every mismatch instead would refuse the frames already in flight
    when a node changes its configuration — a re-PUT loop rather than a
    recovery — and would make config_stale on the ack unreachable.
    """
    token, first = register(server)
    moved = dataclasses.replace(OWL, rx_lat=51.5)
    _, put, _ = post(f"{server.url}/nodes/config", to_wire(build_node_config(moved)), token, "PUT")
    assert put["config_version"] == first + 1

    status, body, _ = post(f"{server.url}/nodes/detection", frame(first), token)

    assert status == 202
    assert body == {"accepted": 1, "config_stale": True, "streaming_allowed": True}


def test_a_config_put_of_the_registered_configuration_does_not_move_the_version(server):
    """Registration stores the configuration it carried, so the node is already
    at that version and the first PUT is a resend.

    The old mock stored nothing at registration, so its first PUT always bumped
    — which quietly hid the resend path a node actually takes on every start.
    """
    token, version = register(server)

    _, body, _ = post(
        f"{server.url}/nodes/config", to_wire(build_node_config(OWL)), token, method="PUT"
    )

    assert body["config_version"] == version
    _, beat_body, _ = post(f"{server.url}/nodes/heartbeat", beat(version), token)
    assert beat_body["config_stale"] is False


# ── it records what it saw ───────────────────────────────────────────


def test_requests_are_recorded_for_assertions(server):
    token, version = register(server)
    post(f"{server.url}/nodes/detection", frame(version, seq=7), token)

    detections = server.received("detection")

    assert len(detections) == 1
    assert detections[0].body["seq"] == 7
    assert detections[0].bearer == token


def test_an_unknown_endpoint_is_a_404_not_a_crash(server):
    status, _, _ = post(f"{server.url}/nodes/nonsense", {})

    assert status == 404


def test_two_servers_get_different_ports(server):
    """Binding port 0 means tests can run in parallel without collisions."""
    with MockServer() as other:
        assert other.port != server.port


def test_an_unchanged_configuration_does_not_create_a_version(server):
    """The spec: a new version only if the configuration differs from the
    active one, and the active version returned either way."""
    token, _ = register(server)
    payload = to_wire(build_node_config(OWL))

    _, first, _ = post(f"{server.url}/nodes/config", payload, token, method="PUT")
    _, second, _ = post(f"{server.url}/nodes/config", payload, token, method="PUT")

    assert first["config_version"] == second["config_version"]


def test_a_changed_configuration_creates_a_version(server):
    import dataclasses

    token, _ = register(server)
    _, first, _ = post(
        f"{server.url}/nodes/config",
        to_wire(build_node_config(OWL)),
        token,
        method="PUT",
    )

    moved = dataclasses.replace(OWL, rx_lat=51.5)
    _, second, _ = post(
        f"{server.url}/nodes/config",
        to_wire(build_node_config(moved)),
        token,
        method="PUT",
    )

    assert second["config_version"] == first["config_version"] + 1


def test_an_oversized_heartbeat_is_refused_before_parsing(server):
    """The spec caps heartbeat bodies at 8 KiB at the origin, ahead of parsing.
    The mock enforces it now because schema validation alone accepted a 10 KiB
    beat that production would have refused unread — and the node carrying
    twenty distinct long faults is exactly the one whose beat matters."""
    token, version = register(server)
    bloated = beat(version) | {"errors": ["x" * 512] * 20}

    status, body, _ = post(f"{server.url}/nodes/heartbeat", bloated, token)

    assert status == 413
    # The server's own taxonomy, not the spec's `payload_too_large`. The
    # contract declares no 413 anywhere, so the body is the server's choice.
    assert body == {"error": "too_large"}


def test_the_errors_accumulator_never_produces_one(server):
    """The corresponding node-side bound. Errors are dropped least-frequent
    first until the rendered array fits, so this is the accumulator's own
    output going through the same check."""
    from retina_telemetry.errors import DEFAULT_LIMIT, Errors
    from retina_telemetry.wire.heartbeat import build_heartbeat

    token, version = register(server)
    errors = Errors()
    for i in range(DEFAULT_LIMIT):
        errors.add(f"distinct fault {i} " + "x" * 600)

    payload = to_wire(
        build_heartbeat(
            state="streaming",
            uptime_s=1,
            config_version=version,
            boot_id=BOOT_ID,
            errors=errors.take().messages,
        )
    )
    status, _, _ = post(f"{server.url}/nodes/heartbeat", payload, token)

    assert status == 200


def test_a_full_detection_frame_is_within_its_larger_cap(server):
    """512 detections across four parallel arrays is legitimately large, which
    is why the spec allows a detection frame 64 KiB rather than 8."""
    token, version = register(server)
    full = frame(version) | {
        "delay": [123.456] * 512,
        "doppler": [-1188.88] * 512,
        "snr": [14.25] * 512,
        "adsb_hex": ["4ca1f2"] * 512,
    }

    status, _, _ = post(f"{server.url}/nodes/detection", full, token)

    assert status == 202


# ── the divergences from the contract, made reachable ────────────────
#
# Five places where Tower-Finder's implementation departs from the spec, all of
# them reachable by a healthy node. These tests exist so the departures are
# rehearsed here rather than discovered on a real board.


def test_the_two_401_shapes(server):
    """The same condition answers in two shapes depending on which path met it.

    PUT /nodes/config catches its own dependency and restores the contract's
    `Error`; detection and heartbeat take that dependency through `Depends` and
    let FastAPI render its own. Our client keys off the status, so it survives
    both — which is exactly what this pins.
    """
    register(server)

    _, detection, _ = post(f"{server.url}/nodes/detection", frame(), "tok_wrong")
    _, heartbeat, _ = post(f"{server.url}/nodes/heartbeat", beat(), "tok_wrong")
    _, config, _ = post(
        f"{server.url}/nodes/config", to_wire(build_node_config(OWL)), "tok_wrong", "PUT"
    )

    assert detection == heartbeat == {"detail": "unauthorized"}
    assert config == {"error": "unauthorized"}


def test_a_bad_token_beats_a_malformed_body(server):
    """Dependencies are solved before the body is validated, so 401 wins over
    422. The config path reads its body by hand for the same reason: a
    body-shaped refusal ahead of identity resolution would sort live tokens
    from dead ones."""
    register(server)
    broken = frame()
    del broken["adsb_hex"]

    status, _, _ = post(f"{server.url}/nodes/detection", broken, "tok_wrong")

    assert status == 401


def test_re_registration_revokes_the_previous_token(server):
    """The reflash path. A board that comes back keeps its node_ref and its
    configuration version, and its previous token dies."""
    first, version = register(server)

    second, again = register(server)

    assert second != first
    assert again == version
    assert post(f"{server.url}/nodes/detection", frame(version), first)[0] == 401
    assert post(f"{server.url}/nodes/detection", frame(version), second)[0] == 202


def test_registration_is_refused_with_403_not_429_when_the_allowance_is_spent(server):
    """Five an hour per node_id, and every admitted attempt counts — including
    one that turns out to name an identity Mender has not accepted.

    A 429 would tell a caller its identity is known enough to be counted, so
    the limiter shares the one opaque refusal with every other class.
    """
    for _ in range(5):
        assert register(server)

    status, body, headers = post(f"{server.url}/nodes/register", _registration())

    assert status == 403
    assert body == {"error": "forbidden"}
    assert 240 <= int(headers["Retry-After"]) <= 359


def test_every_refusal_class_is_one_body(server):
    """Unknown device, awaiting acceptance, Mender unreachable and rate limited
    are indistinguishable, at the same latency, by design."""
    bodies = set()
    for reason in ("unknown", "pending", "down"):
        server.state.mender = reason
        status, body, headers = post(f"{server.url}/nodes/register", _registration())
        assert status == 403
        assert int(headers["Retry-After"]) > 0
        bodies.add(json.dumps(body, sort_keys=True))

    assert bodies == {json.dumps({"error": "forbidden"}, sort_keys=True)}


def test_the_detection_rate_limit_is_a_429_carrying_retry_after(frozen_server):
    """Eight a second, sized against the contract's 2 Hz ceiling. Owl runs at
    0.6-0.9 Hz, so a node never reaches this — but a retry loop would.

    On a frozen clock, so that all nine land in one window by construction
    rather than by being quick enough.
    """
    server = frozen_server
    token, version = register(server)

    admitted = [post(f"{server.url}/nodes/detection", frame(version), token)[0] for _ in range(8)]
    status, body, headers = post(f"{server.url}/nodes/detection", frame(version), token)

    assert admitted == [202] * 8
    assert status == 429
    assert body == {"error": "rate_limited"}
    assert int(headers["Retry-After"]) >= 1


def test_the_detection_allowance_refreshes_on_the_window_boundary(frozen_server, clock):
    """The other side of the same coin, and the one that made CI red.

    Windows are aligned on the clock rather than sliding from first use, so a
    burst crossing a boundary is handed a full allowance part way through. Real
    behaviour, pinned deliberately here — it used to be reachable only by a
    runner slow enough to blunder into it.
    """
    server = frozen_server
    token, version = register(server)
    for _ in range(8):
        post(f"{server.url}/nodes/detection", frame(version), token)
    assert post(f"{server.url}/nodes/detection", frame(version), token)[0] == 429

    clock.advance(1)

    assert post(f"{server.url}/nodes/detection", frame(version), token)[0] == 202


def test_the_heartbeat_keeps_its_own_allowance(frozen_server):
    """Keyed on (node_id, endpoint), so a node streaming at its ceiling can
    still be heard from. The beat is the one thing a node in trouble has left."""
    server = frozen_server
    token, version = register(server)
    for _ in range(8):
        post(f"{server.url}/nodes/detection", frame(version), token)

    assert post(f"{server.url}/nodes/detection", frame(version), token)[0] == 429
    assert post(f"{server.url}/nodes/heartbeat", beat(version), token)[0] == 200


# ── the config validator's three unspecifiable checks ────────────────


def test_a_degenerate_baseline_is_refused_against_tx_lat(server):
    """Not in the spec and not expressible in a JSON schema: a receiver and
    illuminator at the same point give the solver nothing to work with. Our
    models cannot catch it, so it arrives as a 400 from the server."""
    token, _ = register(server)
    same = to_wire(build_node_config(OWL))
    same["tx_lat"], same["tx_lon"] = same["rx_lat"], same["rx_lon"]

    status, body, _ = post(f"{server.url}/nodes/config", same, token, "PUT")

    assert status == 400
    assert body == {"error": "invalid_config", "detail": "tx_lat"}


def test_an_unknown_field_is_named_back_and_bounded(server):
    """The rejected field is caller-supplied JSON, and `Error.detail` is capped
    at 512. Passed through whole it would fail the server's own response model
    and turn a 400 the node must not retry into a 500 it will."""
    token, _ = register(server)
    key = "z" * 600

    status, body, _ = post(
        f"{server.url}/nodes/config", to_wire(build_node_config(OWL)) | {key: 1}, token, "PUT"
    )

    assert status == 400
    assert body == {"error": "invalid_config", "detail": "z" * 512}


def test_a_body_that_is_not_an_object_is_a_400_naming_config(server):
    token, _ = register(server)

    status, body, _ = post(
        f"{server.url}/nodes/config", [to_wire(build_node_config(OWL))], token, "PUT"
    )

    assert status == 400
    assert body == {"error": "invalid_config", "detail": "config"}
