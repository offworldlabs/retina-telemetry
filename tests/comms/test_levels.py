"""The shared level applier — one function, every endpoint.

Levels rather than edges: the server restates all of them on every response, so
missing one is harmless and reading it twice is too.
"""

import pytest

from retina_telemetry.comms.client import Kind, Outcome
from retina_telemetry.comms.levels import apply_response
from retina_telemetry.state import State


@pytest.fixture
def state(tmp_path):
    state = State(tmp_path / "token")
    state.store_token("tok_abc", node_ref="nd_original", config_version=7)
    state.config_resend.clear()
    return state


def outcome(kind=Kind.OK, status=200, **body):
    return Outcome(kind=kind, status=status, body=body or None, retry_after_s=None, error=None)


# ── the two rules that matter most ───────────────────────────────────


def test_a_401_never_triggers_re_registration(state):
    """Revocation is a 401 rather than a control field because it has to work
    whether or not the node cooperates. Answering it by registering again turns
    one deliberate revocation into a storm."""
    apply_response(outcome(Kind.UNAUTHORIZED, 401), state)

    snapshot = state.snapshot()
    assert snapshot.token == "tok_abc"  # kept, not cleared
    assert snapshot.token_rejected
    assert not snapshot.may_stream
    assert snapshot.may_heartbeat  # keeps the failure visible


def test_a_409_asks_for_a_config_resend(state):
    apply_response(outcome(Kind.CONFLICT, 409), state)

    assert state.config_resend.is_set()
    assert state.snapshot().config_stale


def test_a_409_does_not_disturb_the_token(state):
    apply_response(outcome(Kind.CONFLICT, 409), state)

    assert not state.snapshot().token_rejected


# ── levels from a body ───────────────────────────────────────────────


def test_a_detection_ack_applies_its_levels(state):
    apply_response(
        outcome(accepted=2, config_stale=False, streaming_allowed=False),
        state,
    )

    assert not state.snapshot().streaming_allowed


def test_a_heartbeat_response_applies_the_same_levels(state):
    """Identical handling — which is why this is one function, not four."""
    apply_response(
        outcome(
            server_time="2026-08-10T09:00:00Z",
            config_stale=True,
            streaming_allowed=True,
            node_ref="nd_rotated",
        ),
        state,
    )

    snapshot = state.snapshot()
    assert snapshot.config_stale
    assert snapshot.node_ref == "nd_rotated"
    assert state.config_resend.is_set()


def test_a_config_response_adopts_the_active_version(state):
    apply_response(outcome(config_version=12), state)

    assert state.snapshot().config_version == 12


def test_an_absent_level_is_not_false(state):
    """None means the response did not carry the field."""
    state.apply_levels(streaming_allowed=False)

    apply_response(outcome(accepted=1), state)  # says nothing about streaming

    assert not state.snapshot().streaming_allowed


def test_a_bodyless_response_applies_nothing(state):
    before = state.snapshot()

    apply_response(Outcome(Kind.OK, 202, None, None, None), state)

    after = state.snapshot()
    assert (after.config_version, after.streaming_allowed) == (
        before.config_version,
        before.streaming_allowed,
    )


def test_junk_values_are_ignored_rather_than_adopted(state):
    apply_response(
        outcome(config_version="seven", streaming_allowed="yes", node_ref=""),
        state,
    )

    snapshot = state.snapshot()
    assert snapshot.config_version == 7
    assert snapshot.streaming_allowed
    assert snapshot.node_ref == "nd_original"


# ── clock offset ─────────────────────────────────────────────────────


def test_server_time_sets_the_clock_offset(state):
    apply_response(outcome(server_time="2026-08-10T09:00:00Z"), state)

    assert state.snapshot().clock_offset_s is not None


def test_a_z_suffix_is_accepted(state):
    """RFC 3339 as the spec writes it."""
    apply_response(outcome(server_time="2026-08-10T09:00:00Z"), state)

    assert state.snapshot().clock_offset_s is not None


def test_an_explicit_offset_is_accepted(state):
    apply_response(outcome(server_time="2026-08-10T09:00:00+00:00"), state)

    assert state.snapshot().clock_offset_s is not None


def test_an_unparseable_server_time_is_ignored(state):
    apply_response(outcome(server_time="soon"), state)

    assert state.snapshot().clock_offset_s is None


def test_a_large_offset_is_logged(state, caplog):
    """A freshly flashed board can be badly wrong before NTP settles, and
    detection timestamps are node-clock."""
    with caplog.at_level("WARNING"):
        apply_response(outcome(server_time="2020-01-01T00:00:00Z"), state)

    assert "clock" in caplog.text


def test_a_small_offset_is_not_logged(state, caplog):
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    with caplog.at_level("WARNING"):
        apply_response(outcome(server_time=now), state)

    assert "clock" not in caplog.text


# ── failures ─────────────────────────────────────────────────────────


def test_an_unreachable_outcome_changes_nothing(state):
    before = state.snapshot()

    apply_response(Outcome(Kind.UNREACHABLE, None, None, None, "refused"), state)

    assert state.snapshot().redacted() == before.redacted()


def test_a_rate_limited_outcome_changes_no_levels(state):
    apply_response(Outcome(Kind.RATE_LIMITED, 429, {"error": "slow down"}, 30, None), state)

    assert state.snapshot().streaming_allowed
