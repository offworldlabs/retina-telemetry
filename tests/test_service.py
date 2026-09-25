"""The wiring, end to end against the mock.

These are the only tests that exercise all three layers together, which makes
them the ones that catch a payload the pieces each considered fine.
"""

import contextlib
import dataclasses
import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from retina_telemetry.__main__ import CLAIM_ASK_FRESH_FOR_S, Service
from retina_telemetry.collect.tracker import TrackerFrame, TrackRaw
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
        tracker_url="http://127.0.0.1:1",  # nor a tracker
        token_path=node / "token",
        status_path=node / "status.json",
        node_id_path=node / "node_id",
        device_type_path=node / "device_type",
        consent_path=node / "consent.json",
        contact_path=node / "contact.json",
        claim_path=node / "claim.json",
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


def unsite(node):
    """Strip the geometry the way retina-node's default.yml ships it: the keys
    present and every value null, so "unset" stays distinguishable from "this
    configuration is malformed"."""
    document = yaml.safe_load((node / "config.yml").read_text())
    for end in ("rx", "tx"):
        document["location"][end] = {
            "latitude": None,
            "longitude": None,
            "altitude": None,
            "name": None,
        }
    (node / "config.yml").write_text(yaml.safe_dump(document))


def test_an_unsited_node_registers_with_explicit_nulls(node, server):
    """The whole point of phase two. Under v1.1.1 this node held registration
    and the fleet could not see it at all; the server now counts it, streams
    from it, and places nothing on the map until a position arrives."""
    unsite(node)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("register"))

    config = server.requests[0].body["config"]
    assert config["rx_lat"] is None
    assert config["rx_lon"] is None
    assert config["tx_alt_ft"] is None
    # Present and null, never dropped: a required-and-nullable key's absence is
    # a payload the server rejects.
    assert {"rx_lat", "rx_lon", "rx_alt_ft", "tx_lat", "tx_lon", "tx_alt_ft"} <= set(config)
    assert "tx_callsign" in config


def test_an_unsited_node_names_no_illuminator(node, server):
    """A tower's name and its position are set at the same wizard step, so the
    node with no position has no name for one either. v1.2.2 made the field
    nullable so that says itself, rather than a placeholder standing in."""
    unsite(node)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("register"))

    assert server.requests[0].body["config"]["tx_callsign"] is None


def test_an_unsited_node_is_not_reported_as_a_broken_config(node, server):
    """The ordinary state of a new node, not a fault. Reading the geometry
    with _require made every unsited node look unreadable, which is a support
    call rather than a setup step."""
    unsite(node)
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.6)

    detail = status(node)["detail"]
    assert "position is configured" in detail
    assert not any("could not be read" in e for e in status(node).get("errors", []))


def test_an_unsited_node_still_says_so_once_it_is_registered(node, server):
    """It now looks entirely healthy from outside: registered, streaming and
    heartbeating. The status detail is the only thing telling an operator why
    nothing of theirs appears on the map."""
    unsite(node)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("register"))

    assert "position is configured" in status(node)["detail"]


def test_siting_a_node_takes_effect_without_a_restart(node, server):
    """retina-gui writes the config into a container already running. The node
    registered while unsited, so what has to travel now is the configuration
    change, not a held registration."""
    original = (node / "config.yml").read_text()
    unsite(node)
    service = Service(settings_for(node, server))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.received("register"):
        time.sleep(0.02)

    (node / "config.yml").write_text(original)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not _sited_config_sent(server):
        time.sleep(0.02)
    service.shutdown()
    thread.join(timeout=5)

    assert server.received("register")
    assert _sited_config_sent(server), "the new position never reached the server"


def _sited_config_sent(server):
    return any(
        request.endpoint == "config" and request.body.get("rx_lat") is not None
        for request in server.requests
    )


# ── the contact document ─────────────────────────────────────────────
#
# Optional throughout. The spec says a node with nothing to report never calls
# the endpoint at all, so the silence cases matter more than the sending one.

CONTACT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+441234567890",
    "country": "GB",
}


def write_contact(node, document):
    (node / "contact.json").write_text(json.dumps(document))


def test_a_node_with_no_contact_details_never_calls_the_endpoint(node, server):
    """Not "sends an empty document": an empty document is a valid payload
    that clears whatever the server holds, and a node that has never had any
    details must not volunteer to clear a record it never wrote."""
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("config"))

    assert not server.received("contact")


def test_contact_details_are_sent_once_registered(node, server):
    write_contact(node, CONTACT)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("contact"))

    sent = [r for r in server.requests if r.endpoint == "contact"][0]
    assert sent.body == CONTACT


def test_the_same_details_are_not_sent_twice(node, server):
    """On local change only. Nothing on the server asks for this and no
    response marks it stale, so a repeat would be pure noise."""
    write_contact(node, CONTACT)
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.2)

    assert len(server.received("contact")) == 1


def test_editing_the_details_sends_them_again(node, server):
    write_contact(node, CONTACT)
    service = Service(settings_for(node, server))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.received("contact"):
        time.sleep(0.02)

    write_contact(node, {**CONTACT, "phone": "+15551234567"})
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(server.received("contact")) < 2:
        time.sleep(0.02)
    service.shutdown()
    thread.join(timeout=5)

    assert len(server.received("contact")) == 2
    assert server.received("contact")[-1].body["phone"] == "+15551234567"


def test_clearing_the_details_reaches_the_server(node, server):
    """The endpoint replaces wholesale, so an empty document is how a removal
    travels. This is the one case where sending empty is right."""
    write_contact(node, CONTACT)
    service = Service(settings_for(node, server))

    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.received("contact"):
        time.sleep(0.02)

    write_contact(node, {})
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and len(server.received("contact")) < 2:
        time.sleep(0.02)
    service.shutdown()
    thread.join(timeout=5)

    assert server.received("contact")[-1].body == {}


def test_a_rejected_contact_document_reaches_errors_and_not_the_detail(node, server):
    """A refused contact breaks nothing: the node registers, streams and beats
    exactly as before, and the only loss is a way to ring the owner. `detail`
    is for what stops a node working."""
    write_contact(node, CONTACT)
    server.enqueue("contact", 400, body={"error": "invalid_contact", "detail": "email"})
    service = Service(settings_for(node, server))

    def reported():
        return any(
            any("contact" in e for e in (r.body.get("errors") or []))
            for r in server.requests
            if r.endpoint == "heartbeat"
        )

    run_briefly(service, until=reported)

    assert reported(), "the refusal has to reach the server through errors[]"
    assert "contact" not in (status(node)["detail"] or "")


def test_a_rejected_contact_document_does_not_stop_the_configuration(node, server):
    """The two ride the same loop, so a refusal on one must not cost the other."""
    write_contact(node, CONTACT)
    server.enqueue("contact", 400, body={"error": "invalid_contact", "detail": "email"})
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("config"))

    assert server.received("config")


def test_an_unreadable_config_does_not_stop_the_contact_details(node, server):
    """A node whose config.yml is broken can still say who owns it, and that
    is exactly the node somebody needs to ring."""
    write_contact(node, CONTACT)
    service = Service(settings_for(node, server))
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.received("register"):
        time.sleep(0.02)
    (node / "config.yml").write_text("this is not: [valid yaml")

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not server.received("contact"):
        time.sleep(0.02)
    service.shutdown()
    thread.join(timeout=5)

    assert server.received("contact")


# ── the claim ────────────────────────────────────────────────────────
#
# Two calls chosen here rather than by retina-gui, because this is the only
# side that knows what the server does with each. The case that makes the
# second call exist is a declined link: the address stays on file, so offering
# it again is accepted, changes nothing and mails nothing.

CLAIM_ADDRESS = "owner@example.com"


def write_claim(node, **document):
    (node / "claim.json").write_text(json.dumps({"email": CLAIM_ADDRESS, **document}))


def just_now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextlib.contextmanager
def service_running(service):
    """Run the service for the body, so a test can change a file mid-run."""
    service.stop.clear()
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        service.stop.set()
        thread.join(timeout=5)


def wait_for(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.05)
    return predicate()


def test_a_node_nobody_claims_never_calls_the_endpoint(node, server):
    """The ordinary state. An unclaimed node registers, streams and beats
    exactly as a claimed one does."""
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("config"))

    assert not server.received("claim")


def test_an_address_is_offered_once_registered(node, server):
    write_claim(node)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: server.received("claim"))

    assert server.received("claim")[0].body == {"email": CLAIM_ADDRESS}


def test_the_same_address_is_not_offered_twice(node, server):
    """On local change only, like the contact document."""
    write_claim(node)
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.2)

    assert len(server.received("claim")) == 1


def test_a_changed_address_is_offered_again(node, server):
    write_claim(node)
    service = Service(settings_for(node, server))

    with service_running(service):
        assert wait_for(lambda: server.received("claim"))
        write_claim(node, email="someone.else@example.com")
        assert wait_for(lambda: len(server.received("claim")) == 2)

    assert server.received("claim")[-1].body == {"email": "someone.else@example.com"}


def test_an_ask_resends_rather_than_offering_again(node, server):
    """The declined-link case, and the whole reason the second call exists.

    The address has not changed, so a PUT would be accepted, change nothing and
    mail nothing. Only the resend produces another link.
    """
    write_claim(node)
    service = Service(settings_for(node, server))

    with service_running(service):
        assert wait_for(lambda: server.received("claim"))
        write_claim(node, send_requested_at=just_now())
        assert wait_for(lambda: server.received("claim_resend"))

    assert len(server.received("claim")) == 1  # the address never changed
    assert len(server.received("claim_resend")) == 1


def test_a_released_node_is_left_alone_until_asked(node, server):
    """A release says the node is not the owner's any more.

    They may have released it to give it away, so mailing them a link to take
    it back unasked would be the wrong answer. The address stays in the file
    after a release, and must not count as a change.
    """
    write_claim(node)
    service = Service(settings_for(node, server))

    with service_running(service):
        assert wait_for(lambda: server.received("claim"))
        server.release()
        time.sleep(0.8)  # several heartbeats and claim ticks

    assert len(server.received("claim")) == 1
    assert not server.received("claim_resend")


def test_an_ask_on_a_released_node_offers_the_address(node, server):
    """Found on jonathan-node-1: claimed, released from the dashboard, and
    then Send again produced no link.

    A release clears the address, and a resend mails only the address on file,
    so it mailed nothing. On a node the server holds no address for, an ask is
    answered with an offer instead.
    """
    write_claim(node)
    service = Service(settings_for(node, server))

    with service_running(service):
        assert wait_for(lambda: server.received("claim"))
        server.release()
        assert wait_for(lambda: service.state.snapshot().claim.email is None)
        write_claim(node, send_requested_at=just_now())
        assert wait_for(lambda: len(server.received("claim")) == 2)
        time.sleep(0.6)  # the ask is acted on once

    assert server.received("claim")[-1].body == {"email": CLAIM_ADDRESS}
    assert len(server.received("claim")) == 2
    assert not server.received("claim_resend")


def test_an_ask_is_acted_on_once(node, server):
    """It stays in the file, so acting on it every tick would mail the owner
    every tick."""
    write_claim(node)
    service = Service(settings_for(node, server))

    with service_running(service):
        assert wait_for(lambda: server.received("claim"))
        write_claim(node, send_requested_at=just_now())
        assert wait_for(lambda: server.received("claim_resend"))
        time.sleep(0.6)  # several more ticks

    assert len(server.received("claim_resend")) == 1


def test_a_stale_ask_is_ignored(node, server):
    """Nothing durable records that we acted, so without an age bound a
    restart would mail the owner another link every time it came up."""
    write_claim(node)
    service = Service(settings_for(node, server))
    old = datetime.now(UTC) - timedelta(seconds=CLAIM_ASK_FRESH_FOR_S + 60)

    with service_running(service):
        assert wait_for(lambda: server.received("claim"))
        write_claim(node, send_requested_at=old.strftime("%Y-%m-%dT%H:%M:%SZ"))
        time.sleep(0.6)

    assert not server.received("claim_resend")


def test_an_ask_stored_with_a_first_offer_does_not_mail_twice(node, server):
    """An owner who typed a new address and pressed Send link, which stamps
    an ask beside it on every press.

    The offer is itself the call that mails, so the timestamp beside it has
    already been answered and must not produce a second link.
    """
    write_claim(node, send_requested_at=just_now())
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.2)

    assert len(server.received("claim")) == 1
    assert not server.received("claim_resend")


def test_a_claim_failure_never_reaches_the_status_document(node, server):
    """Nothing about the claim stops a node working, so a refusal belongs in
    `errors[]` and never in `detail`, which is for what does."""
    write_claim(node, email="not-an-address")
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.2)

    document = json.loads((node / "status.json").read_text())
    # `detail` still describes the node's own state, which here is a healthy
    # node waiting on its first frame. What must not be in it is the claim.
    assert "claim" not in (document["detail"] or "")


def test_a_refused_address_is_not_offered_again(node, server):
    """`invalid_claim` means repeating it cannot help. A corrected address is
    what tries again."""
    write_claim(node, email="not-an-address")
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=1.2)

    assert len(server.received("claim")) == 1


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


# ── a refused registration has to reach the operator ─────────────────
#
# It did not, for as long as this service has existed. `errors[]` carried the
# refusal and nothing reads that list: retina-gui renders `detail` and ignores
# the rest, so owl-ded9 showed its owner a blank line while being refused 232
# times over nine days with a 400 that named the broken field.


def test_a_rejected_registration_names_the_field_in_the_detail(node, server):
    """The actionable refusal. A 400 cannot clear until somebody edits the
    configuration, so the field it names must reach the one line an operator
    reads."""
    server.enqueue("register", 400, body={"error": "invalid_config", "detail": "tx_lat"}, count=5)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: status(node).get("detail"))

    detail = status(node)["detail"]
    assert "tx_lat" in detail
    # Named page, not the app: this sentence is rendered by retina-gui itself,
    # so "fix it in retina-gui" is advice to somebody already looking at it.
    assert "Configuration page" in detail


def test_a_refused_registration_says_so_from_the_first_refusal(node, server):
    """No threshold. The 403 wording carries "this is normal on a new node"
    itself, rather than a delay doing that job and leaving a stuck node
    looking healthy in the meantime."""
    server.enqueue("register", 403, retry_after=1, count=10)
    service = Service(settings_for(node, server))

    run_briefly(service, until=lambda: status(node).get("detail"))

    detail = status(node)["detail"]
    assert "refused" in detail
    assert "normal" in detail, "a newly flashed node is refused as a matter of course"


def test_an_unreachable_server_is_reported_as_the_network_rather_than_the_node(node, server):
    """Nothing is wrong with the node, and telling an owner to check their
    configuration would send them after the wrong thing."""
    unreachable = dataclasses.replace(settings_for(node, server), api_url="http://127.0.0.1:1/v1")
    service = Service(unreachable)

    run_briefly(service, until=lambda: status(node).get("detail"))

    assert "cannot reach the server" in status(node)["detail"]


def test_a_registration_refusal_clears_once_the_node_registers(node, server):
    """A stale refusal on a working node is worse than none."""
    server.enqueue("register", 403, retry_after=1)
    service = Service(settings_for(node, server))

    run_briefly(
        service, until=lambda: server.received("register") and service.state.snapshot().token
    )

    assert service._registration_refused is None
    assert "refused" not in (status(node)["detail"] or "")


def test_an_unregistered_node_says_something_even_before_a_refusal(node, server):
    """`unregistered` had no sentence at all, so the window before the first
    attempt was blank too."""
    (node / "consent.json").unlink()
    service = Service(settings_for(node, server))

    run_briefly(service, seconds=0.6)

    assert status(node)["detail"]


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
    # The fake reports no association, so the frame carries neither column.
    # That is how a node says it matched nothing, since contract 1.5.0.
    assert "adsb" not in sent
    assert "adsb_hex" not in sent


def test_frames_carry_the_trackers_tracks(node, server, monkeypatch):
    """Contract 1.6.0, end to end: the tracker's answer for the polled frame
    rides on that frame, and the mock, which refuses tracks that do not add up
    exactly as the server does, accepts it."""
    service = Service(settings_for(node, server, retina_tracker="v0.4.0"))
    monkeypatch.setattr(
        service.blah2, "poll_detection", lambda: _poll(frame(int(time.time() * 1000)))
    )
    asked = []

    def tracker_frame(timestamp_ms):
        asked.append(timestamp_ms)
        return TrackerFrame(
            run="20260925T101500Z-3fa9c1",
            timestamp_ms=timestamp_ms,
            tracks=[
                TrackRaw(
                    id="260925-00001A",
                    state="active",
                    hit=1,
                    n_associated=9,
                    n_missed=0,
                    adsb_hex="4ca1f2",
                    is_anomalous=False,
                    anomaly_types=[],
                    max_velocity_ms=231.4,
                    born_timestamp_ms=timestamp_ms - 5000,
                    avg_snr_db=16.2,
                    shadow_fraction=0.0,
                    interference_fraction=0.0,
                )
            ],
        )

    monkeypatch.setattr(service.tracker, "frame", tracker_frame)

    run_briefly(
        service,
        until=lambda: server.received("detection") and server.received("heartbeat"),
    )

    request = server.received("detection")[-1]
    assert request.body["tracker"] == {"run": "20260925T101500Z-3fa9c1"}
    (track,) = request.body["tracks"]
    assert track["hit"] == 1
    assert track["state"] == "active"
    # Asked for the very frame it rides on, not whatever the tracker holds now.
    assert round(request.body["t"] * 1000) in asked
    beat = server.received("heartbeat")[-1].body
    assert beat["versions"]["retina_tracker"] == "v0.4.0"


def test_a_node_without_a_tracker_streams_as_before(node, server, monkeypatch):
    """settings_for points the tracker at a dead port. Frames go out without
    either field, and the reason reaches errors[] once, not once per frame."""
    service = Service(settings_for(node, server))
    monkeypatch.setattr(
        service.blah2, "poll_detection", lambda: _poll(frame(int(time.time() * 1000)))
    )

    run_briefly(service, until=lambda: len(server.received("detection")) >= 3)

    sent = server.received("detection")[-1].body
    assert "tracker" not in sent and "tracks" not in sent
    batch = service.errors.take()
    assert len([m for m in batch.messages if "tracker unreachable" in m]) == 1


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
