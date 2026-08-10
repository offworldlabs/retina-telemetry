"""The machinery, against the real mock over real HTTP."""

import random

import pytest

from retina_telemetry.comms.client import Backoff, Client, Kind
from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.serialise import to_wire
from tests.wire.test_config import OWL
from tools.mock_server import MockServer

REGISTRATION = {
    "node_id": "ret824685c9",
    "board_model": "pi5-v3-arm64",
    "agreement": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
    "config": to_wire(build_node_config(OWL)),
}


@pytest.fixture
def server():
    with MockServer() as running:
        yield running


@pytest.fixture
def client(server):
    return Client(server.url)


def register(client):
    outcome = client.post("/nodes/register", REGISTRATION)
    assert outcome.ok
    return outcome.body["token"], outcome.body["config_version"]


def frame(config_version):
    return {
        "t": 1786014064.679,
        "seq": 1,
        "config_version": config_version,
        "delay": [41.362],
        "doppler": [-118.0],
        "snr": [14.2],
        "adsb_hex": [None],
    }


# ── classification ───────────────────────────────────────────────────


def test_a_successful_registration_is_ok(client):
    outcome = client.post("/nodes/register", REGISTRATION)

    assert outcome.kind is Kind.OK
    assert outcome.status == 200
    assert outcome.body["token"]


def test_a_missing_token_is_unauthorized(client, server):
    token, version = register(client)

    outcome = client.post("/nodes/detection", frame(version))

    assert outcome.kind is Kind.UNAUTHORIZED
    assert not outcome.retryable


def test_a_bad_payload_is_invalid(client):
    outcome = client.post("/nodes/register", {"node_id": "Unknown"})

    assert outcome.kind is Kind.INVALID
    assert not outcome.retryable


def test_a_stale_config_version_is_a_conflict(client):
    token, _ = register(client)

    outcome = client.post("/nodes/detection", frame(99), token=token)

    assert outcome.kind is Kind.CONFLICT


def test_a_conflict_is_not_retryable(client):
    """The same request would fail identically until a config resend."""
    token, _ = register(client)

    assert not client.post("/nodes/detection", frame(99), token=token).retryable


def test_rate_limiting_is_retryable_and_carries_retry_after(client, server):
    token, version = register(client)
    server.enqueue("detection", 429, retry_after=30)

    outcome = client.post("/nodes/detection", frame(version), token=token)

    assert outcome.kind is Kind.RATE_LIMITED
    assert outcome.retryable
    assert outcome.retry_after_s == 30


def test_a_refusal_is_retryable(client, server):
    """403 is the normal answer while the Mender mirror catches up."""
    server.enqueue("register", 403, retry_after=60)

    outcome = client.post("/nodes/register", REGISTRATION)

    assert outcome.kind is Kind.REFUSED
    assert outcome.retryable
    assert outcome.retry_after_s == 60


def test_a_server_error_is_retryable(client, server):
    server.enqueue("register", 503)

    outcome = client.post("/nodes/register", REGISTRATION)

    assert outcome.kind is Kind.SERVER_ERROR
    assert outcome.retryable


def test_an_unreachable_server_never_raises(server):
    """No transport failure may escape — the loops must not die."""
    unreachable = Client("http://127.0.0.1:1", timeout_s=0.5)

    outcome = unreachable.post("/nodes/heartbeat", {})

    assert outcome.kind is Kind.UNREACHABLE
    assert outcome.retryable
    assert outcome.status is None
    assert "unreachable" in outcome.error


def test_a_timeout_is_unreachable_not_an_exception(server):
    """A detection request past its timeout is abandoned so the next frame
    goes out fresh."""
    slow = Client("http://10.255.255.1", timeout_s=(0.3, 0.3))

    assert slow.post("/nodes/detection", {}).kind is Kind.UNREACHABLE


# ── auth ─────────────────────────────────────────────────────────────


def test_the_token_is_sent_as_a_bearer(client, server):
    token, version = register(client)

    client.post("/nodes/detection", frame(version), token=token)

    assert server.received("detection")[-1].bearer == token


def test_registration_sends_no_authorization_header(client, server):
    client.post("/nodes/register", REGISTRATION)

    assert server.received("register")[-1].authorization is None


def test_the_token_never_appears_in_an_error_string(client, server):
    token, version = register(client)
    server.enqueue("detection", 500, body={"error": "boom"})

    outcome = client.post("/nodes/detection", frame(version), token=token)

    assert token not in outcome.describe()


# ── one connection ───────────────────────────────────────────────────


def test_requests_reuse_one_connection(client, server):
    """One kept-alive connection per node — cheaper than a TLS handshake per
    frame at 2 Hz. Counted server-side, since that is the thing that matters
    and it does not depend on urllib3 internals."""
    token, version = register(client)
    for _ in range(5):
        client.post("/nodes/detection", frame(version), token=token)

    assert len(server.requests) == 6
    assert server.connections == 1


def test_a_separate_client_opens_its_own_connection(server):
    """Proving the previous test measures something."""
    Client(server.url).post("/nodes/register", REGISTRATION)
    Client(server.url).post("/nodes/register", REGISTRATION)

    assert server.connections == 2


# ── backoff ──────────────────────────────────────────────────────────


def test_backoff_grows_exponentially():
    backoff = Backoff(base_s=1.0, factor=2.0, jitter=0.0)

    assert [backoff.next_delay() for _ in range(4)] == [1.0, 2.0, 4.0, 8.0]


def test_backoff_is_capped():
    backoff = Backoff(base_s=1.0, factor=10.0, maximum_s=5.0, jitter=0.0)

    assert [backoff.next_delay() for _ in range(3)] == [1.0, 5.0, 5.0]


def test_backoff_resets():
    backoff = Backoff(base_s=1.0, factor=2.0, jitter=0.0)
    backoff.next_delay()
    backoff.next_delay()

    backoff.reset()

    assert backoff.next_delay() == 1.0


def test_jitter_spreads_a_fleet_restarting_together():
    """A CGNAT pool rebooting in lockstep is a case the server sizes for."""
    delays = {Backoff(base_s=10.0, rng=random.Random(seed)).next_delay() for seed in range(50)}

    assert len(delays) > 40  # essentially all distinct


def test_jitter_never_produces_a_near_zero_delay():
    """Registration is limited to 5/hour per node, so a delay near zero would
    burn the budget in minutes. This is why it is not "full jitter"."""
    for seed in range(200):
        assert Backoff(base_s=10.0, rng=random.Random(seed)).next_delay() >= 5.0


def test_jitter_stays_within_the_cap():
    for seed in range(200):
        backoff = Backoff(base_s=100.0, maximum_s=100.0, rng=random.Random(seed))
        assert backoff.next_delay() <= 100.0
