"""The wiring, end to end against the mock.

These are the only tests that exercise all three layers together, which makes
them the ones that catch a payload the pieces each considered fine.
"""

import json
import threading
import time

import pytest
import yaml

from retina_telemetry.__main__ import Service
from retina_telemetry.comms.lifecycle import NodeState
from retina_telemetry.settings import Settings
from tests.collect.test_node_config import DEFAULTS
from tests.fakes.blah2_api import frame
from tools.mock_server import MockServer

CONSENT = {
    "opted_in": True,
    "agreement": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
}


@pytest.fixture
def server():
    with MockServer() as running:
        yield running


@pytest.fixture
def node(tmp_path):
    """A node with every local precondition satisfied."""
    (tmp_path / "node_id").write_text("ret824685c9\n")
    (tmp_path / "device_type").write_text("device_type=pi5-v3-arm64\n")
    (tmp_path / "consent.json").write_text(json.dumps(CONSENT))

    document = json.loads(json.dumps(DEFAULTS))  # deep copy
    document["location"]["rx"]["beam_width"] = 60  # Q1 has not landed on a real node
    (tmp_path / "config.yml").write_text(yaml.safe_dump(document))
    return tmp_path


def settings_for(node, server, **overrides):
    return Settings(
        api_url=server.url,
        blah2_url="http://127.0.0.1:1",  # no blah2 unless a test provides one
        token_path=node / "token",
        status_path=node / "status.json",
        node_id_path=node / "node_id",
        device_type_path=node / "device_type",
        consent_path=node / "consent.json",
        config_path=node / "config.yml",
        disk_path=node,
        poll_interval_s=0.05,
        heartbeat_interval_s=0.2,
        config_poll_s=0.1,
        status_interval_s=0.05,
        **overrides,
    )


def run_briefly(service, seconds=2.0, until=None):
    """Run the service in a thread, stopping as soon as `until` holds."""
    service.stop.clear()
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if until and until():
            break
        time.sleep(0.02)
    service.shutdown()
    thread.join(timeout=5)


def status(node):
    """The document, or empty if it has not been written yet."""
    path = node / "status.json"
    return json.loads(path.read_text()) if path.exists() else {}


# ── the happy path ───────────────────────────────────────────────────


def test_a_node_registers_and_heartbeats(node, server):
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("heartbeat"))

    assert server.received("register")
    assert server.received("heartbeat")
    assert service.state.snapshot().registered


def test_it_sends_its_configuration_after_registering(node, server):
    """Registration returns a config_version, but the node still resends on
    start because it does not persist one."""
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("config"))

    assert server.received("config")


def test_the_token_is_persisted(node, server):
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: service.state.snapshot().registered)

    assert (node / "token").read_text().strip().startswith("tok_")


def test_a_restart_reuses_the_token_without_re_registering(node, server):
    first = Service(settings_for(node, server))
    run_briefly(first, until=lambda: first.state.snapshot().registered)
    registrations = len(server.received("register"))

    second = Service(settings_for(node, server))
    run_briefly(second, until=lambda: server.received("heartbeat"))

    assert len(server.received("register")) == registrations
    assert second.state.snapshot().registered


# ── the status document ──────────────────────────────────────────────


def test_the_status_document_is_written_immediately(node, server):
    """Before anything else happens — a node that cannot register must still
    explain itself."""
    Service(settings_for(node, server)).write_status()

    assert status(node)["node_id"] == "ret824685c9"


def test_the_status_document_never_contains_the_token(node, server):
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: service.state.snapshot().registered)

    assert service.state.snapshot().token not in (node / "status.json").read_text()


def test_node_ref_reaches_the_status_document(node, server):
    """The only path by which an owner ever learns their public identifier."""
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: status(node).get("node_ref"))

    assert status(node)["node_ref"]


# ── blocked nodes ────────────────────────────────────────────────────


def test_an_opted_out_node_sends_nothing(node, server):
    (node / "consent.json").write_text(json.dumps({"opted_in": False}))
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.8)

    assert server.requests == []
    assert status(node)["state"] == NodeState.OPTED_OUT


def test_a_node_with_no_consent_record_sends_nothing(node, server):
    """Every node in the fleet today — nothing writes the file yet."""
    (node / "consent.json").unlink()
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.8)

    assert server.requests == []


def test_a_node_with_no_identity_says_so(node, server):
    (node / "node_id").unlink()
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.8)

    assert server.requests == []
    document = status(node)
    assert document["state"] == NodeState.NO_IDENTITY
    assert document["node_id"] is None
    assert "Mender" in document["detail"]


def test_a_node_without_beam_geometry_cannot_register(node, server):
    """Q1 — the real state of every node in the fleet."""
    document = yaml.safe_load((node / "config.yml").read_text())
    del document["location"]["rx"]["beam_width"]
    (node / "config.yml").write_text(yaml.safe_dump(document))

    run_briefly(Service(settings_for(node, server)), seconds=0.8)

    assert server.received("register") == []


def test_fixing_consent_takes_effect_without_a_restart(node, server):
    """The facts are re-read rather than cached, so an operator opting in does
    not have to bounce the container."""
    (node / "consent.json").write_text(json.dumps({"opted_in": False}))
    service = Service(settings_for(node, server))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert server.requests == []

    (node / "consent.json").write_text(json.dumps(CONSENT))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.received("register"):
        time.sleep(0.02)
    service.shutdown()
    thread.join(timeout=5)

    assert server.received("register")


# ── the server pushing back ──────────────────────────────────────────


def test_a_revoked_token_stops_the_stream_but_not_the_beat(node, server):
    server.enqueue("heartbeat", 401, count=10)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: service.state.snapshot().token_rejected)

    assert service.state.snapshot().token_rejected
    assert not service.state.snapshot().may_stream
    assert service.state.snapshot().may_heartbeat  # keeps the failure visible


def test_it_never_re_registers_after_a_revocation(node, server):
    """Treating a 401 as a reason to register again turns one deliberate
    revocation into a registration storm."""
    server.enqueue("heartbeat", 401, count=20)
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.5, until=lambda: service.state.snapshot().token_rejected)

    assert len(server.received("register")) == 1


def test_a_refused_registration_backs_off_rather_than_hot_looping(node, server):
    """The per-node limit is 5/hour, so a hot loop burns the budget in minutes."""
    server.enqueue("register", 403, retry_after=1, count=10)

    run_briefly(Service(settings_for(node, server)), seconds=1.5)

    assert len(server.received("register")) <= 2


def test_a_config_rejection_reaches_the_operator(node, server):
    """A 400 means the node cannot stream at all until somebody edits the
    configuration, so it must be visible rather than retried into silence."""
    service = Service(settings_for(node, server))
    server.enqueue("config", 400, body={"error": "invalid_request", "detail": "rx_lat"}, count=5)

    run_briefly(
        service, seconds=2.0, until=lambda: "rejected" in (status(node).get("detail") or "")
    )

    assert "rejected" in status(node)["detail"]


# ── detections ───────────────────────────────────────────────────────


def test_frames_are_sent_when_blah2_is_available(node, server, monkeypatch):
    """The whole chain: poll, build, convert, serialise, send."""
    service = Service(settings_for(node, server))
    monkeypatch.setattr(
        service.blah2, "poll_detection", lambda: _poll(frame(int(time.time() * 1000)))
    )

    run_briefly(service, until=lambda: server.received("detection"))

    sent = server.received("detection")[-1].body
    assert sent["delay"] == [41.362, 100.403]  # km converted to microseconds
    assert sent["seq"] >= 1
    assert sent["adsb_hex"] == [None, None]


def test_no_detections_is_reported_rather_than_claiming_to_stream(node, server):
    """blah2 is unreachable in this fixture, so the node is permitted to stream
    and has nothing to send."""
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: status(node).get("state") == NodeState.NO_DETECTIONS)

    assert status(node)["state"] == NodeState.NO_DETECTIONS


def _poll(payload):
    from retina_telemetry.collect.blah2 import parse_frame

    return parse_frame(payload)


# ── shutdown ─────────────────────────────────────────────────────────


def test_shutdown_is_prompt(node, server):
    service = Service(settings_for(node, server))
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.3)

    started = time.monotonic()
    service.shutdown()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert time.monotonic() - started < 6.0


def test_the_final_status_is_written_on_shutdown(node, server):
    service = Service(settings_for(node, server))
    run_briefly(service, seconds=0.5)

    assert status(node)["written_at"]
