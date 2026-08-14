import pytest

from retina_telemetry.comms.client import Backoff, Client, Kind
from retina_telemetry.comms.lifecycle import NodeState, Registrar, derive_state, explain
from tests.comms.conftest import REGISTRATION


@pytest.fixture
def state(unregistered):
    """These tests drive registration themselves, so they start from nothing."""
    return unregistered


@pytest.fixture
def registrar(server, state):
    return Registrar(Client(server.url), state, backoff=Backoff(base_s=1.0, jitter=0.0))


def ready(state, **overrides):
    """The facts that hold for a fully working node."""
    return {
        "has_identity": True,
        "licence_accepted": True,
        "all_records_present": True,
        **overrides,
    }


# ── the state is derived, not stored ─────────────────────────────────


def test_an_opted_out_node_reports_that_first(state):
    """Precedence runs from what we control least to most. A missing identity
    on an opted-out node is not worth reporting — nothing would be done."""
    derived = derive_state(
        state.snapshot(), **ready(state, licence_accepted=False, has_identity=False)
    )

    assert derived is NodeState.OPTED_OUT


def test_a_missing_identity_outranks_a_missing_agreement(state):
    derived = derive_state(
        state.snapshot(), **ready(state, has_identity=False, all_records_present=False)
    )

    assert derived is NodeState.NO_IDENTITY


def test_a_missing_agreement_blocks_registration(state):
    assert (
        derive_state(state.snapshot(), **ready(state, all_records_present=False))
        is NodeState.NO_AGREEMENT
    )


def test_everything_present_but_no_token(state):
    assert derive_state(state.snapshot(), **ready(state)) is NodeState.UNREGISTERED


def test_a_request_in_flight_is_the_one_thing_passed_in(state):
    """Not derivable from state that outlives the request."""
    derived = derive_state(state.snapshot(), **ready(state), registering=True)

    assert derived is NodeState.REGISTERING


def test_a_restarted_node_awaits_config(state):
    """Every node passes through this, since config_version is not persisted."""
    state.store_token("tok_abc", node_ref="nd1", config_version=7)
    state.config_resent(config_version=7)
    with state._lock:  # noqa: SLF001 - reproducing a restart
        state._config_version = None

    assert derive_state(state.snapshot(), **ready(state)) is NodeState.AWAITING_CONFIG


def test_a_registered_node_streams(state):
    state.store_token("tok_abc", node_ref="nd1", config_version=7)

    assert derive_state(state.snapshot(), **ready(state)) is NodeState.STREAMING


def test_a_paused_node_is_not_streaming(state):
    state.store_token("tok_abc", node_ref="nd1", config_version=7)
    state.apply_levels(streaming_allowed=False)

    assert derive_state(state.snapshot(), **ready(state)) is NodeState.PAUSED


def test_a_revoked_token_outranks_awaiting_config(state):
    """ "Revoked" is the more actionable thing to report, and a node whose token
    was refused is not waiting for configuration in any useful sense."""
    state.store_token("tok_abc", node_ref="nd1", config_version=7)
    with state._lock:  # noqa: SLF001
        state._config_version = None
    state.reject_token()

    assert derive_state(state.snapshot(), **ready(state)) is NodeState.REVOKED


def test_a_radar_that_has_never_produced_is_starting(state):
    """The spec's word for exactly this window: "before the radar has produced
    anything, which is where a new owner most often needs support"."""
    state.store_token("tok_abc", node_ref="nd1", config_version=7)

    derived = derive_state(
        state.snapshot(), **ready(state), detections_flowing=False, ever_detected=False
    )

    assert derived is NodeState.STARTING
    assert derived.wire.value == "starting"


def test_a_radar_that_has_stopped_is_stalled_not_an_error(state):
    """A working node with a stopped radar. v1.1.1 added `stalled` for exactly
    this, because `error` made the server raise against the node when the fault
    is the radar's — and `starting` is not true of something that has already
    run."""
    state.store_token("tok_abc", node_ref="nd1", config_version=7)

    derived = derive_state(
        state.snapshot(), **ready(state), detections_flowing=False, ever_detected=True
    )

    assert derived is NodeState.NO_DETECTIONS
    assert derived.wire.value == "stalled"
    assert derived.reaches_the_server


def test_detections_flowing_is_streaming(state):
    state.store_token("tok_abc", node_ref="nd1", config_version=7)

    assert (
        derive_state(state.snapshot(), **ready(state), detections_flowing=True)
        is NodeState.STREAMING
    )


def test_not_having_polled_yet_does_not_downgrade(state):
    """None means we have not looked. Reporting no detections before looking
    would be a guess, and the first poll is a second away."""
    state.store_token("tok_abc", node_ref="nd1", config_version=7)

    assert (
        derive_state(state.snapshot(), **ready(state), detections_flowing=None)
        is NodeState.STREAMING
    )


def test_being_paused_outranks_having_no_detections(state):
    """Told to stop is a more useful thing to report than nothing to send."""
    state.store_token("tok_abc", node_ref="nd1", config_version=7)
    state.apply_levels(streaming_allowed=False)

    assert (
        derive_state(state.snapshot(), **ready(state), detections_flowing=False) is NodeState.PAUSED
    )


def test_the_derived_state_cannot_disagree_with_may_stream(state):
    """The whole reason it is derived rather than assigned."""
    state.store_token("tok_abc", node_ref="nd1", config_version=7)

    for allowed in (True, False):
        state.apply_levels(streaming_allowed=allowed)
        snapshot = state.snapshot()
        derived = derive_state(snapshot, **ready(state))
        assert (derived is NodeState.STREAMING) == snapshot.may_stream


# ── which states the server ever sees ────────────────────────────────


def test_only_states_a_node_can_report_reach_the_server():
    """The others describe a node that cannot build a heartbeat at all."""
    reaching = {s for s in NodeState if s.reaches_the_server}

    assert reaching == {
        NodeState.STREAMING,
        NodeState.STARTING,
        NodeState.NO_DETECTIONS,
        NodeState.PAUSED,
        NodeState.REVOKED,
    }


def test_every_state_maps_onto_the_spec_s_closed_set(state):
    """Ours is richer because the status document can report things the wire
    cannot. Everything must still land on one of their five."""
    from retina_telemetry.wire.models import NodeState as WireState

    assert {s.wire for s in NodeState} <= set(WireState)


def test_a_revoked_token_is_an_error_on_the_wire():
    assert NodeState.REVOKED.wire.value == "error"


def test_states_that_never_reach_the_server_still_map_to_something_true():
    """`starting` is true of all of them, and none is ever sent."""
    for state in (NodeState.OPTED_OUT, NodeState.NO_IDENTITY, NodeState.AWAITING_CONFIG):
        assert state.wire.value == "starting"


def test_every_blocked_state_explains_itself():
    """The status document is the only route to an operator, so a bare enum
    value is not enough."""
    for state in (
        NodeState.OPTED_OUT,
        NodeState.NO_IDENTITY,
        NodeState.NO_AGREEMENT,
        NodeState.STARTING,
        NodeState.NO_DETECTIONS,
        NodeState.REVOKED,
    ):
        assert explain(state)


def test_streaming_needs_no_explanation():
    assert explain(NodeState.STREAMING) is None


# ── registration ─────────────────────────────────────────────────────


def test_a_successful_registration_stores_everything(registrar, state):
    outcome = registrar.attempt(REGISTRATION)

    assert outcome.ok
    snapshot = state.snapshot()
    assert snapshot.token
    assert snapshot.config_version >= 1
    assert snapshot.node_ref


def test_a_refusal_leaves_the_node_unregistered(registrar, state, server):
    server.enqueue("register", 403, retry_after=60)

    outcome = registrar.attempt(REGISTRATION)

    assert outcome.kind is Kind.REFUSED
    assert not state.snapshot().registered


def test_retry_after_wins_over_our_own_backoff(registrar, server):
    """It is required on a 403 and is the server telling us what it wants."""
    server.enqueue("register", 403, retry_after=90)

    outcome = registrar.attempt(REGISTRATION)

    assert registrar.delay_before_retry(outcome) == 90.0


def test_our_backoff_advances_even_when_retry_after_is_honoured(registrar, server):
    """So a server that stops sending the header does not reset us to a fast
    loop."""
    server.enqueue("register", 403, retry_after=90, count=2)

    registrar.delay_before_retry(registrar.attempt(REGISTRATION))
    registrar.delay_before_retry(registrar.attempt(REGISTRATION))
    server.enqueue("register", 503)

    assert registrar.delay_before_retry(registrar.attempt(REGISTRATION)) == 4.0


def test_backoff_is_used_when_no_header_is_sent(registrar, server):
    server.enqueue("register", 503)

    assert registrar.delay_before_retry(registrar.attempt(REGISTRATION)) == 1.0


def test_a_successful_registration_resets_the_backoff(registrar, server):
    server.enqueue("register", 503, count=3)
    for _ in range(3):
        registrar.delay_before_retry(registrar.attempt(REGISTRATION))

    registrar.attempt(REGISTRATION)

    assert registrar.attempts == 0


def test_repeated_refusals_do_not_hot_loop(registrar, server):
    """The per-node limit is 5/hour, so a broken retry loop burns the budget in
    minutes. The delays must grow."""
    server.enqueue("register", 403, count=4)

    delays = [registrar.delay_before_retry(registrar.attempt(REGISTRATION)) for _ in range(4)]

    assert delays == sorted(delays)
    assert delays[-1] > delays[0]


def test_a_400_is_surfaced_rather_than_retried(registrar, server, caplog):
    """Retrying unchanged will not help — the configuration failed validation."""
    server.enqueue("register", 400, body={"error": "invalid_request", "detail": "rx_lat"})

    with caplog.at_level("ERROR"):
        outcome = registrar.attempt(REGISTRATION)

    assert outcome.kind is Kind.INVALID
    assert not outcome.retryable
    assert "rejected" in caplog.text


# ── a server breaking its own contract ───────────────────────────────


def test_a_200_without_a_token_is_treated_as_a_failure(registrar, state, server):
    """Storing nothing and reporting success would strand the node silently."""
    server.enqueue("register", 200, body={"node_ref": "nd1", "config_version": 1})

    outcome = registrar.attempt(REGISTRATION)

    assert not outcome.ok
    assert not state.snapshot().registered
    assert "no token" in outcome.error


def test_a_200_without_a_config_version_is_treated_as_a_failure(registrar, state, server):
    server.enqueue("register", 200, body={"token": "tok_abc", "node_ref": "nd1"})

    outcome = registrar.attempt(REGISTRATION)

    assert not outcome.ok
    assert not state.snapshot().registered


def test_a_missing_node_ref_does_not_block_registration(registrar, state, server):
    """Display only — losing it must not strand a node that has a token."""
    server.enqueue("register", 200, body={"token": "tok_abc", "config_version": 3})

    outcome = registrar.attempt(REGISTRATION)

    assert outcome.ok
    assert state.snapshot().config_version == 3


def test_attempt_never_sleeps(registrar, server):
    """Retrying is the caller's loop so a stop signal can interrupt it."""
    import time

    server.enqueue("register", 403, retry_after=3600)

    started = time.monotonic()
    registrar.attempt(REGISTRATION)

    assert time.monotonic() - started < 1.0
