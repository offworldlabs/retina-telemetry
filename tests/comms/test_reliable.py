import threading
import time

import pytest

from retina_telemetry.comms.client import Backoff, Client, Kind
from retina_telemetry.comms.reliable import is_fatal_for_config, send_until_delivered
from tests.comms.conftest import CONFIG


@pytest.fixture
def stop():
    return threading.Event()


def send(server, state, stop, payload=None, **kwargs):
    return send_until_delivered(
        Client(server.url),
        "PUT",
        "/nodes/config",
        lambda: payload if payload is not None else CONFIG,
        state=state,
        stop=stop,
        token=state.snapshot().token,
        backoff=kwargs.pop("backoff", Backoff(base_s=0.01, jitter=0.0)),
        **kwargs,
    )


def beat(config_version=1):
    return {"state": "streaming", "uptime_s": 1, "config_version": config_version}


# ── it lands ─────────────────────────────────────────────────────────


def test_a_successful_send_returns_immediately(server, state, stop):
    outcome = send(server, state, stop)

    assert outcome.ok
    assert len(server.received("config")) == 1


def test_it_retries_until_delivered(server, state, stop):
    """The opposite of the detection stream: losing this leaves the server
    unable to interpret anything that follows."""
    server.enqueue("config", 503, count=3)

    outcome = send(server, state, stop)

    assert outcome.ok
    assert len(server.received("config")) == 4


def test_retry_after_is_honoured_over_our_backoff(server, state, stop):
    server.enqueue("config", 429, retry_after=1)

    started = time.monotonic()
    send(server, state, stop, max_attempts=2)

    assert time.monotonic() - started >= 1.0


def test_a_successful_send_resets_the_backoff(server, state, stop):
    backoff = Backoff(base_s=0.01, jitter=0.0)
    server.enqueue("config", 503, count=2)

    send(server, state, stop, backoff=backoff)

    assert backoff.attempts == 0


# ── it gives up when repeating cannot help ───────────────────────────


def test_a_400_is_not_retried(server, state, stop):
    """Retrying unchanged will not help — the configuration failed validation."""
    server.enqueue("config", 400, body={"error": "invalid_request"}, count=5)

    outcome = send(server, state, stop)

    assert outcome.kind is Kind.INVALID
    assert len(server.received("config")) == 1


def test_a_400_on_config_is_fatal_for_the_operator(server, state, stop):
    """The node cannot stream at all until somebody edits the configuration,
    so it has to reach the status document."""
    server.enqueue("config", 400, body={"error": "invalid_request"})

    assert is_fatal_for_config(send(server, state, stop))


def test_a_401_is_not_retried_and_marks_the_token(server, state, stop):
    server.enqueue("config", 401, count=5)

    outcome = send(server, state, stop)

    assert outcome.kind is Kind.UNAUTHORIZED
    assert len(server.received("config")) == 1
    assert state.snapshot().token_rejected


def test_a_declining_factory_abandons_the_send(server, state, stop):
    outcome = send_until_delivered(
        Client(server.url),
        "PUT",
        "/nodes/config",
        lambda: None,
        state=state,
        stop=stop,
    )

    assert outcome is None
    assert server.received("config") == []


# ── the payload is rebuilt every attempt ─────────────────────────────


def test_the_payload_is_rebuilt_before_each_attempt(server, state, stop):
    """A heartbeat retried two minutes later should carry the health and uptime
    it has now, not the ones it had when the first attempt failed."""
    server.enqueue("heartbeat", 503, count=2)
    uptimes = iter([10, 20, 30])

    send_until_delivered(
        Client(server.url),
        "POST",
        "/nodes/heartbeat",
        lambda: beat() | {"uptime_s": next(uptimes)},
        state=state,
        stop=stop,
        token=state.snapshot().token,
        backoff=Backoff(base_s=0.01, jitter=0.0),
    )

    assert [r.body["uptime_s"] for r in server.received("heartbeat")] == [10, 20, 30]


# ── shutdown is prompt ───────────────────────────────────────────────


def test_stopping_interrupts_a_long_backoff(server, state, stop):
    """A node told to shut down while waiting out a 30-minute registration
    backoff must not take 30 minutes to notice."""
    server.enqueue("config", 503, count=10)
    threading.Timer(0.2, stop.set).start()

    started = time.monotonic()
    send(server, state, stop, backoff=Backoff(base_s=30.0, jitter=0.0))

    assert time.monotonic() - started < 5.0


def test_an_already_stopped_sender_does_nothing(server, state, stop):
    stop.set()

    assert send(server, state, stop) is None
    assert server.received("config") == []


# ── levels still apply ───────────────────────────────────────────────


def test_levels_are_applied_on_success(server, state, stop):
    before = state.snapshot().config_version

    send(server, state, stop)

    assert state.snapshot().config_version == before + 1  # the mock bumps on a PUT


def test_levels_are_applied_on_failure_too(server, state, stop):
    """A 409 or 401 is exactly the sort of thing that must reach state."""
    server.enqueue("config", 401)

    send(server, state, stop)

    assert state.snapshot().token_rejected
