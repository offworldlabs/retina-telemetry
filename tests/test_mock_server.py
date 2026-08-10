"""The mock is test infrastructure, so it needs testing itself.

A mock that silently accepts a malformed payload would let a real bug through
stage 3 unnoticed, which is worse than having no mock at all.
"""

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


def register(server):
    status, body, _ = post(
        f"{server.url}/nodes/register",
        {
            "node_id": "ret824685c9",
            "board_model": "pi5-v3-arm64",
            "agreement": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
            "config": to_wire(build_node_config(OWL)),
        },
    )
    assert status == 200
    return body["token"], body["config_version"]


def frame(config_version=1, seq=1):
    return {
        "t": 1786014064.679,
        "seq": seq,
        "config_version": config_version,
        "delay": [41.362],
        "doppler": [-118.0],
        "snr": [14.2],
        "adsb_hex": [None],
    }


def beat(config_version=1):
    return {"state": "streaming", "uptime_s": 181569, "config_version": config_version}


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
    """The mock validates against the same generated models the client builds
    with, so the spec's own pattern applies here too."""
    status, body, _ = post(
        f"{server.url}/nodes/register",
        {
            "node_id": "Unknown",
            "board_model": "x",
            "agreement": {"version": "1", "accepted_at": "2026-07-31T09:12:00Z"},
            "config": to_wire(build_node_config(OWL)),
        },
    )

    assert status == 400
    assert "pattern" in body["detail"].lower()


def test_a_frame_missing_a_required_array_is_rejected(server):
    token, version = register(server)
    broken = frame(version)
    del broken["adsb_hex"]

    status, _, _ = post(f"{server.url}/nodes/detection", broken, token)

    assert status == 400


def test_a_config_without_beam_azimuth_is_rejected(server):
    """The field is required and nullable. This is the mock proving that
    exclude_none=True would have produced a payload the server refuses."""
    token, _ = register(server)
    dropped = build_node_config(OWL).model_dump(exclude_none=True)

    status, _, _ = post(f"{server.url}/nodes/config", dropped, token, method="PUT")

    assert status == 400


def test_the_correctly_serialised_config_is_accepted(server):
    token, _ = register(server)
    status, body, _ = post(
        f"{server.url}/nodes/config", to_wire(build_node_config(OWL)), token, method="PUT"
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
    """config_stale and streaming_allowed are levels restated on every
    response, not edges, so a node that missed one still learns."""
    token, version = register(server)

    with server.state.lock:
        server.state.streaming_allowed = False

    _, body, _ = post(f"{server.url}/nodes/detection", frame(version), token)

    assert body["streaming_allowed"] is False


def test_a_stale_config_version_gets_409(server):
    token, _ = register(server)

    status, _, _ = post(f"{server.url}/nodes/detection", frame(config_version=99), token)

    assert status == 409


def test_a_config_put_bumps_the_version_and_clears_stale(server):
    token, version = register(server)
    with server.state.lock:
        server.state.config_stale = True

    _, body, _ = post(
        f"{server.url}/nodes/config", to_wire(build_node_config(OWL)), token, method="PUT"
    )

    assert body["config_version"] == version + 1
    _, beat_body, _ = post(f"{server.url}/nodes/heartbeat", beat(body["config_version"]), token)
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
