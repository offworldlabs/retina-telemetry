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
from tests.conftest import CONSENT_FILE as CONSENT
from tests.fakes.blah2_api import frame
from tools.mock_server import MockServer


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
    (tmp_path / "setup-wizard-completed").write_text("2026-06-30T08:18:00")

    document = json.loads(json.dumps(DEFAULTS))  # deep copy
    document["location"]["rx"]["beam_width"] = 60  # no real node has this set
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
        wizard_flag_path=node / "setup-wizard-completed",
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
    (node / "consent.json").write_text(json.dumps({}))
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


def test_a_node_without_beam_geometry_registers_with_nulls(node, server):
    """The real state of every node in the fleet, and it must not strand them.
    retina-gui is not collecting the geometry, so this is the default path —
    and since v1.1.1 the keys travel as explicit nulls rather than being
    dropped, which is what the server needs to distinguish "not characterised"
    from a payload someone forgot to populate."""
    document = yaml.safe_load((node / "config.yml").read_text())
    del document["location"]["rx"]["beam_width"]
    (node / "config.yml").write_text(yaml.safe_dump(document))

    service = Service(settings_for(node, server))
    run_briefly(service, seconds=1.0, until=lambda: service.state.snapshot().registered)

    sent = server.received("register")
    assert len(sent) == 1
    assert sent[0].body["config"]["beam_width_deg"] is None
    assert sent[0].body["config"]["beam_azimuth_deg"] is None


def test_fixing_consent_takes_effect_without_a_restart(node, server):
    """The facts are re-read rather than cached, so an operator opting in does
    not have to bounce the container."""
    (node / "consent.json").write_text(json.dumps({}))
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


def test_an_unfinished_wizard_registers_nothing(node, server):
    """Until the wizard is done the config is the shipped Greenwich/Crystal
    Palace default, and registering would tell the server that is where the
    node is. Silence is the correct behaviour."""
    (node / "setup-wizard-completed").unlink()
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.6)

    assert server.requests == []
    assert status(node)["state"] == "setup_incomplete"


def test_finishing_the_wizard_takes_effect_without_a_restart(node, server):
    """retina-gui writes the flag while this container is already running, so
    a node that completes setup must not need bouncing to register."""
    (node / "setup-wizard-completed").unlink()
    service = Service(settings_for(node, server))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert server.requests == []

    (node / "setup-wizard-completed").write_text("2026-08-26T11:54:00")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.received("register"):
        time.sleep(0.02)
    service.shutdown()
    thread.join(timeout=5)

    assert server.received("register")


def test_the_wizard_gate_does_not_stop_the_status_document(node, server):
    """A node that can do nothing else must still say so: the status document
    is the only channel out of this container."""
    (node / "setup-wizard-completed").unlink()
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.6)

    document = status(node)
    assert document["state"] == "setup_incomplete"
    assert "wizard" in document["detail"]


def test_an_unsited_node_registers_nothing(node, server):
    """It has no geometry to register with, and the wire cannot carry a null
    one yet. Silence beats asserting a position nobody chose."""
    document = yaml.safe_load((node / "config.yml").read_text())
    for end in ("rx", "tx"):
        document["location"][end] = {
            "latitude": None,
            "longitude": None,
            "altitude": None,
            "name": None,
        }
    (node / "config.yml").write_text(yaml.safe_dump(document))
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.6)

    assert server.requests == []


def test_an_unsited_node_is_not_reported_as_a_broken_config(node, server):
    """The ordinary state of a new node, not a fault. Reading the geometry
    with _require made every unsited node look unreadable, which is a support
    call rather than a setup step."""
    document = yaml.safe_load((node / "config.yml").read_text())
    document["location"]["rx"]["latitude"] = None
    (node / "config.yml").write_text(yaml.safe_dump(document))
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.6)

    detail = status(node)["detail"]
    assert "position is configured" in detail
    assert not any("could not be read" in e for e in status(node).get("errors", []))


def test_siting_a_node_takes_effect_without_a_restart(node, server):
    """retina-gui writes the config into a container already running."""
    original = (node / "config.yml").read_text()
    document = yaml.safe_load(original)
    document["location"]["rx"]["latitude"] = None
    (node / "config.yml").write_text(yaml.safe_dump(document))
    service = Service(settings_for(node, server))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert server.requests == []

    (node / "config.yml").write_text(original)
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


def test_a_node_whose_radar_never_started_says_starting(node, server):
    """blah2 is unreachable in this fixture and has never produced a frame,
    which is precisely what the spec's `starting` describes — not a claim to be
    streaming while nothing arrives."""
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: status(node).get("state") == NodeState.STARTING)

    assert status(node)["state"] == NodeState.STARTING


def test_a_superseded_version_is_recovered_without_a_409(node, server, monkeypatch):
    """The path the old mock could not produce, and the one a node meets in the
    seconds after any configuration change.

    The server moves its active version underneath a streaming node. The frames
    already in flight carry the old one, which it *issued* — so they are accepted
    with `config_stale` rather than refused, and the node resends its
    configuration and carries on. A mock that 409'd on any mismatch tested the
    recovery from the wrong signal entirely.
    """
    service = Service(settings_for(node, server))
    monkeypatch.setattr(
        service.blah2, "poll_detection", lambda: _poll(frame(int(time.time() * 1000)))
    )

    run_briefly(service, until=lambda: server.received("detection"))
    configs_before = len(server.received("config"))
    server.move_config_version(9)

    run_briefly(service, until=lambda: len(server.received("config")) > configs_before)

    # It resent its configuration rather than sitting stale for ever, and
    # adopted the version that came back.
    assert len(server.received("config")) > configs_before
    assert service.state.snapshot().config_version == 10
    # And it went on streaming, at the version it now holds.
    frames_after = len(server.received("detection"))
    run_briefly(service, until=lambda: len(server.received("detection")) > frames_after)
    assert server.received("detection")[-1].body["config_version"] == 10


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


# ── config faults reach the operator ─────────────────────────────────


def test_an_unreadable_config_is_reported_not_crashed(node, server):
    """A node that cannot read its configuration cannot register, and must say
    so rather than exit."""
    (node / "config.yml").write_text("key: [unclosed\n")
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.0, until=lambda: status(node).get("detail"))

    assert "not valid YAML" in status(node)["detail"]
    assert server.received("register") == []


def test_a_missing_config_is_reported(node, server):
    (node / "config.yml").unlink()
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.0, until=lambda: status(node).get("detail"))

    assert "does not exist" in status(node)["detail"]


def test_beam_geometry_removed_after_registration_still_resends(node, server):
    """The key disappearing mid-run is the same as it never being there: the node
    keeps talking and omits it, rather than going quiet over a field the spec no
    longer demands."""
    service = Service(settings_for(node, server))
    run_briefly(service, until=lambda: service.state.snapshot().registered)

    document = yaml.safe_load((node / "config.yml").read_text())
    del document["location"]["rx"]["beam_width"]
    (node / "config.yml").write_text(yaml.safe_dump(document))
    service.state.request_config_resend()

    run_briefly(service, seconds=1.0, until=lambda: server.received("config"))

    resent = server.received("config")
    assert resent, "the resend must still happen"
    assert resent[-1].body["beam_width_deg"] is None


# ── faults that used to kill a loop silently ─────────────────────────


def test_an_out_of_range_config_does_not_kill_registration(node, server):
    """A latitude past 90 parses as YAML and then fails the spec's bounds, so it
    raised ValidationError out of the payload builder. registration_loop had no
    guard, the thread died, and nothing was written anywhere saying why — the
    status document showed `unregistered` with no detail."""
    document = yaml.safe_load((node / "config.yml").read_text())
    document["location"]["rx"]["latitude"] = 91.0
    (node / "config.yml").write_text(yaml.safe_dump(document))

    service = Service(settings_for(node, server))
    run_briefly(service, seconds=1.2, until=lambda: status(node).get("detail"))

    assert server.received("register") == []
    detail = status(node)["detail"]
    assert "rejected" in detail
    assert any("registration" in e for e in status(node)["errors"])


def test_a_dead_loop_is_reported_rather_than_silent(node, server):
    """The guard that makes the two bugs above visible instead of fatal. A
    daemon thread that raises writes nothing to errors[] and nothing to the
    status document, so a node with a stopped loop looks like a node that has
    lost power."""
    service = Service(settings_for(node, server))

    def explode() -> None:
        raise RuntimeError("synthetic fault")

    supervised = service._supervised("heartbeat", explode)
    supervised()  # must not raise

    assert "heartbeat" in service._dead_loops
    assert any("heartbeat loop stopped" in m for m in service.errors.snapshot())

    service.write_status()
    assert "heartbeat" in status(node)["detail"]
    assert "will not recover" in status(node)["detail"]


def test_a_dead_loop_outranks_every_other_detail(node, server):
    """The node is not doing what the rest of the document claims, so this has
    to be the thing an operator reads first."""
    service = Service(settings_for(node, server))
    service._config_rejected = "configuration rejected: something else"

    service._supervised("poll", lambda: (_ for _ in ()).throw(RuntimeError("boom")))()
    service.write_status()

    assert status(node)["detail"].startswith("internal fault")
