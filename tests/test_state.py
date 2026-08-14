import stat
import threading
from datetime import UTC, datetime, timedelta

import pytest

from retina_telemetry.state import State, with_uptime_fallback


@pytest.fixture
def path(tmp_path):
    return tmp_path / "retina-telemetry" / "token"


@pytest.fixture
def state(path):
    return State(path)


def registered(state):
    state.store_token("tok_abc123", node_ref="nde4f2k9xq7m3b8", config_version=7)
    return state


# ── a fresh node ─────────────────────────────────────────────────────


def test_a_node_starts_unregistered(state):
    snapshot = state.snapshot()

    assert not snapshot.registered
    assert snapshot.token is None
    assert not snapshot.may_stream


def test_a_missing_token_file_is_not_an_error(path):
    """Where every node starts."""
    assert not State(path).snapshot().registered


def test_streaming_is_allowed_by_default(state):
    """The server has to tell us to pause; silence is not a pause."""
    assert state.snapshot().streaming_allowed


# ── the token is the only thing on disk ──────────────────────────────


def test_only_the_token_is_written(state, path):
    registered(state)

    assert path.read_text().strip() == "tok_abc123"


def test_the_token_file_is_not_world_readable(state, path):
    """The only secret the node holds."""
    registered(state)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_token_survives_a_restart(state, path):
    registered(state)

    assert State(path).snapshot().token == "tok_abc123"


def test_config_version_does_not_survive_a_restart(state, path):
    """It is re-obtainable from the server via PUT /nodes/config, which needs
    no operator — unlike the token, whose only source is registration."""
    registered(state)

    assert State(path).snapshot().config_version is None


def test_node_ref_does_not_survive_a_restart(state, path):
    """Display only, and the next heartbeat restores it within 60 s."""
    registered(state)

    assert State(path).snapshot().node_ref is None


def test_a_restored_token_asks_for_a_config_resend(state, path):
    """The only way a restarted node gets a config_version back."""
    registered(state)

    assert State(path).config_resend.is_set()


def test_a_restored_node_heartbeats_before_config_lands(state, path):
    """A token alone is enough to beat, and that is what v1.1.1 changed.

    ``HeartbeatRequest.config_version`` became nullable in v1.1.1 precisely so
    the beat is unconditional: a node that can never build a configuration —
    and so can never PUT one, and so can never be issued a version — used to go
    silent, which is the opposite of what you want from a broken node.

    Streaming is still gated, because ``DetectionFrame.config_version`` stays
    required and non-null: a frame cannot be filed without the geometry it was
    measured against.
    """
    registered(state)
    restarted = State(path)

    assert restarted.snapshot().registered
    assert restarted.snapshot().may_heartbeat  # <- inverted by v1.1.1
    assert not restarted.snapshot().may_stream

    restarted.config_resent(config_version=9)

    assert restarted.snapshot().may_heartbeat
    assert restarted.snapshot().may_stream


def test_levels_never_touch_the_disk(state, path):
    registered(state)
    before = path.read_text()

    state.apply_levels(config_version=99, node_ref="nd_rotated", streaming_allowed=False)

    assert path.read_text() == before


def test_an_empty_token_file_is_treated_as_unregistered(path):
    path.parent.mkdir(parents=True)
    path.write_text("\n")

    assert not State(path).snapshot().registered


def test_an_unwritable_path_does_not_crash_the_node(tmp_path):
    """A node that cannot persist still works until it restarts; going silent
    would be worse."""
    blocked = tmp_path / "nope"
    blocked.mkdir()
    blocked.chmod(0o500)
    state = State(blocked / "deeper" / "token")

    try:
        state.store_token("tok_abc", node_ref="nd1", config_version=1)
        assert state.snapshot().token == "tok_abc"  # in memory regardless
    finally:
        blocked.chmod(0o700)


# ── seq ──────────────────────────────────────────────────────────────


def test_seq_starts_at_one(state):
    assert state.next_seq() == 1


def test_seq_increments_once_per_call(state):
    assert [state.next_seq() for _ in range(3)] == [1, 2, 3]


def test_seq_is_not_persisted(state, path):
    """Restart-local by design — persisting would cost an fsync per frame at
    2 Hz on an SD card. boot_id makes the discontinuity explicit instead."""
    registered(state)
    state.next_seq()
    state.next_seq()

    assert State(path).snapshot().seq == 0


def test_seq_is_safe_under_concurrent_use(tmp_path):
    """The counter is the one thing every detection frame touches."""
    state = State(tmp_path / "token")
    taken: list[int] = []
    lock = threading.Lock()

    def take():
        for _ in range(200):
            value = state.next_seq()
            with lock:
                taken.append(value)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(taken) == list(range(1, 1601))  # no duplicates, none lost


# ── a rejected token ─────────────────────────────────────────────────


def test_a_rejected_token_is_kept_not_cleared(state):
    """Never re-register on a 401 — that turns one deliberate revocation into a
    registration storm."""
    registered(state)
    state.reject_token()

    snapshot = state.snapshot()
    assert snapshot.token == "tok_abc123"
    assert snapshot.token_rejected
    assert snapshot.registered


def test_a_rejected_token_stops_streaming(state):
    registered(state)
    assert state.snapshot().may_stream

    state.reject_token()

    assert not state.snapshot().may_stream


def test_a_rejected_token_still_permits_a_heartbeat(state):
    """Continuing to beat is what keeps the failure visible."""
    registered(state)
    state.reject_token()

    assert state.snapshot().may_heartbeat


def test_re_registering_clears_the_rejection(state):
    registered(state)
    state.reject_token()

    state.store_token("tok_new", node_ref="nde4f2k9xq7m3b8", config_version=8)

    assert not state.snapshot().token_rejected


def test_the_token_never_appears_in_a_repr(state):
    registered(state)

    assert "tok_abc123" not in repr(state)
    assert "nde4f2k9xq7m3b8" in repr(state)


def test_the_token_never_appears_in_the_redacted_view(state):
    import json

    registered(state)

    assert "tok_abc123" not in json.dumps(state.snapshot().redacted())


# ── response levels ──────────────────────────────────────────────────


def test_levels_are_applied_atomically(state):
    """A reader must never see a new config_version beside a stale flag from
    before it."""
    registered(state)

    state.apply_levels(config_version=9, config_stale=False, streaming_allowed=True)

    snapshot = state.snapshot()
    assert (snapshot.config_version, snapshot.config_stale) == (9, False)


def test_an_absent_field_is_not_the_same_as_false(state):
    """None means the response did not carry it."""
    registered(state)
    state.apply_levels(streaming_allowed=False)

    state.apply_levels(config_version=8)  # says nothing about streaming

    assert not state.snapshot().streaming_allowed


def test_streaming_stops_when_the_server_says_so(state):
    registered(state)

    state.apply_levels(streaming_allowed=False)

    assert not state.snapshot().may_stream
    assert state.snapshot().may_heartbeat  # a paused node still beats


def test_streaming_resumes_when_the_server_says_so(state):
    registered(state)
    state.apply_levels(streaming_allowed=False)

    state.apply_levels(streaming_allowed=True)

    assert state.snapshot().may_stream


def test_config_stale_wakes_the_config_watcher(state):
    registered(state)
    state.config_resend.clear()

    state.apply_levels(config_stale=True)

    assert state.config_resend.is_set()


def test_a_409_wakes_it_the_same_way(state):
    """Same condition, different route."""
    registered(state)
    state.config_resend.clear()

    state.request_config_resend()

    assert state.config_resend.is_set()
    assert state.snapshot().config_stale


def test_a_successful_resend_clears_the_flag(state):
    registered(state)
    state.request_config_resend()

    state.config_resent(config_version=12)

    snapshot = state.snapshot()
    assert not snapshot.config_stale
    assert snapshot.config_version == 12
    assert not state.config_resend.is_set()


def test_a_rotated_node_ref_is_adopted(state):
    registered(state)

    state.apply_levels(node_ref="nd_rotated")

    assert state.snapshot().node_ref == "nd_rotated"


# ── clock offset ─────────────────────────────────────────────────────


def test_clock_offset_is_measured_against_server_time(state):
    """Detection timestamps are node-clock and a Pi 5 has no battery-backed
    RTC, so a large offset is worth surfacing."""
    behind = datetime.now(UTC) - timedelta(seconds=30)

    state.apply_levels(server_time=behind)

    assert state.snapshot().clock_offset_s == pytest.approx(30, abs=2)


def test_no_offset_before_the_first_response(state):
    assert state.snapshot().clock_offset_s is None


# ── uptime fallback ──────────────────────────────────────────────────


def test_device_uptime_is_used_when_readable(state):
    assert with_uptime_fallback(state.snapshot(), 181569) == 181569


def test_our_own_uptime_is_the_fallback(state):
    """A true lower bound: the board has been up at least as long as we have."""
    resolved = with_uptime_fallback(state.snapshot(), None)

    assert resolved >= 0
    assert resolved == state.snapshot().process_uptime_s
