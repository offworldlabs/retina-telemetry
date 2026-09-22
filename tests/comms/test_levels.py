"""The shared level applier — one function, every endpoint.

Levels rather than edges: the server restates all of them on every response, so
missing one is harmless and reading it twice is too.
"""

import pytest

from retina_telemetry.comms.client import Kind, Outcome
from retina_telemetry.comms.levels import apply_response
from retina_telemetry.state import Claim, State


@pytest.fixture
def state(tmp_path):
    state = State(tmp_path / "token")
    state.store_token("tok_abc", node_ref="nd_original", config_version=7)
    state.config_resend.clear()
    return state


def outcome(kind=Kind.OK, status=200, path="/nodes/heartbeat", **body):
    return Outcome(
        kind=kind, status=status, body=body or None, retry_after_s=None, error=None, path=path
    )


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


# ── the claim block ──────────────────────────────────────────────────


#: The rest of a heartbeat response, so each test below varies only the claim.
LEVELS = {
    "server_time": "2026-09-22T10:00:00Z",
    "config_stale": False,
    "streaming_allowed": True,
    "node_ref": "nde4f2k9xq7m3b8",
}

CLAIM = {
    "claim_state": "pending",
    "claim_email": "owner@example.com",
    "claim_undeliverable": False,
}


def test_a_heartbeat_response_carries_the_claim(state):
    apply_response(outcome(**LEVELS, **CLAIM), state)

    claim = state.snapshot().claim
    assert claim == Claim(state="pending", email="owner@example.com", undeliverable=False)


def test_a_contact_response_carries_it_too(state):
    """The other endpoint that restates it. Same three fields, same handling."""
    apply_response(outcome(updated_at="2026-09-22T10:00:00Z", **CLAIM), state)

    assert state.snapshot().claim.state == "pending"


def test_nothing_is_known_until_a_response_says_so(state):
    """Distinct from `unclaimed`, which is the server saying nobody owns it."""
    assert state.snapshot().claim is None

    apply_response(outcome(**LEVELS), state)  # a heartbeat from before 1.4.0

    assert state.snapshot().claim is None


def test_a_detection_ack_does_not_clear_the_claim(state):
    """The failure this block is read as a unit to prevent.

    A detection ack carries no claim fields at all. Read field by field, its
    absent `claim_email` would be indistinguishable from a cleared one, and the
    ack that arrives seconds after a heartbeat would blank the address.
    """
    apply_response(outcome(**LEVELS, **CLAIM), state)

    apply_response(outcome(accepted=1, config_stale=False, streaming_allowed=True), state)

    assert state.snapshot().claim.email == "owner@example.com"


def test_an_owner_who_releases_the_node_clears_the_address(state):
    """The other half of the same rule: a null *inside* a block that is present
    is a value, and has to be adopted."""
    apply_response(outcome(**LEVELS, **CLAIM), state)

    apply_response(
        outcome(
            **LEVELS,
            claim_state="unclaimed",
            claim_email=None,
            claim_undeliverable=False,
        ),
        state,
    )

    assert state.snapshot().claim == Claim(state="unclaimed", email=None, undeliverable=False)


def test_a_bounced_address_is_carried(state):
    """The one claim value that is actionable: the owner will never get the
    link, and nothing but this says so."""
    apply_response(outcome(**LEVELS, **CLAIM | {"claim_undeliverable": True}), state)

    assert state.snapshot().claim.undeliverable


def test_an_unrecognised_state_is_passed_through(state):
    """Shown to the operator as itself rather than dropped. A fourth value
    would otherwise take the address and the bounce flag down with it."""
    apply_response(outcome(**LEVELS, **CLAIM | {"claim_state": "disputed"}), state)

    assert state.snapshot().claim.state == "disputed"


def test_a_claim_state_that_is_not_a_string_takes_nothing_with_it(state):
    """The gate is `claim_state`, so junk there means no block rather than a
    half-applied one."""
    apply_response(outcome(**LEVELS, **CLAIM | {"claim_state": 7}), state)

    assert state.snapshot().claim is None


# ── the claim endpoints, where a 409 means something else ────────────


CLAIM_200 = {"state": "pending", "email": "owner@example.com", "undeliverable": False}


def test_a_claim_answer_is_adopted(state):
    """`ClaimResponse` names the three without the `claim_` prefix."""
    apply_response(outcome(path="/nodes/claim", **CLAIM_200), state)

    assert state.snapshot().claim == Claim(
        state="pending", email="owner@example.com", undeliverable=False
    )


def test_a_claim_409_reconciles_rather_than_resending_the_config(state):
    """The bug this path exists to prevent.

    A 409 here says the node already has an owner, which says nothing about our
    config_version. Treated as an ordinary conflict it would put a
    PUT /nodes/config on the wire every time somebody offered an address for a
    node that already has one.
    """
    state.config_resend.clear()

    apply_response(
        outcome(
            Kind.CONFLICT,
            409,
            path="/nodes/claim",
            state="owned",
            email="someone.else@example.com",
            undeliverable=False,
        ),
        state,
    )

    assert not state.config_resend.is_set()
    assert not state.snapshot().config_stale
    # It carries the address that won, so the node reconciles without asking.
    assert state.snapshot().claim.email == "someone.else@example.com"
    assert state.snapshot().claim.state == "owned"


def test_a_409_anywhere_else_still_asks_for_a_config_resend(state):
    """The rule the claim path is an exception to, not a replacement for."""
    state.config_resend.clear()

    apply_response(
        outcome(Kind.CONFLICT, 409, path="/nodes/detection", error="unknown_config"), state
    )

    assert state.config_resend.is_set()


def test_a_resend_answer_is_adopted_too(state):
    apply_response(outcome(path="/nodes/claim/resend", **CLAIM_200 | {"state": "owned"}), state)

    assert state.snapshot().claim.state == "owned"


def test_a_refused_claim_leaves_the_last_one_alone(state):
    """`invalid_claim` wears `Error`, so it carries no state to adopt."""
    apply_response(outcome(path="/nodes/claim", **CLAIM_200), state)

    apply_response(outcome(Kind.INVALID, 400, path="/nodes/claim", error="invalid_claim"), state)

    assert state.snapshot().claim.state == "pending"
    assert not state.config_resend.is_set()


def test_a_401_on_a_claim_path_still_rejects_the_token(state):
    """Checked before the path is looked at, so the claim cannot shadow it."""
    apply_response(outcome(Kind.UNAUTHORIZED, 401, path="/nodes/claim"), state)

    assert state.snapshot().token_rejected


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
