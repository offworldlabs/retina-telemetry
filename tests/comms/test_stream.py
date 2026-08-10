import threading

import pytest

from retina_telemetry.comms.client import Client
from retina_telemetry.comms.stream import DetectionStream, Slot


@pytest.fixture
def slot():
    return Slot()


@pytest.fixture
def stream(server, state, slot):
    return DetectionStream(Client(server.url), state, slot)


def frame(seq=1, config_version=1, n=1):
    return {
        "t": 1786014064.679,
        "seq": seq,
        "config_version": config_version,
        "delay": [41.362] * n,
        "doppler": [-118.0] * n,
        "snr": [14.2] * n,
        "adsb_hex": [None] * n,
    }


# ── the slot is the discipline ───────────────────────────────────────


def test_the_slot_holds_one_thing(slot):
    slot.put(frame(1))
    slot.put(frame(2))

    assert slot.take(0.1)["seq"] == 2
    assert slot.take(0.05) is None


def test_writing_replaces_rather_than_rejecting(slot):
    """Not a queue with maxsize 1 — a full queue rejects the newcomer, and the
    newcomer is the one worth keeping."""
    for seq in range(1, 6):
        slot.put(frame(seq))

    assert slot.take(0.1)["seq"] == 5


def test_replaced_frames_are_counted(slot):
    for seq in range(1, 6):
        slot.put(frame(seq))

    assert slot.replaced == 4


def test_taking_an_empty_slot_returns_none(slot):
    """Ordinary: blah2 produces about one frame a second and the sender asks
    more often than that."""
    assert slot.take(0.05) is None


def test_take_blocks_until_something_arrives(slot):
    def publish():
        slot.put(frame(9))

    threading.Timer(0.05, publish).start()

    assert slot.take(2.0)["seq"] == 9


def test_the_slot_is_safe_under_concurrent_writers(slot):
    def publish(start):
        for seq in range(start, start + 100):
            slot.put(frame(seq))

    threads = [threading.Thread(target=publish, args=(n * 100,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert slot.take(0.1) is not None
    assert slot.replaced == 399  # 400 written, one survives


# ── sending ──────────────────────────────────────────────────────────


def test_a_frame_is_sent(stream, slot, server, state):
    slot.put(frame(1, state.snapshot().config_version))

    assert stream.send_pending()
    assert len(server.received("detection")) == 1


def test_nothing_to_send_is_not_a_failure(stream):
    assert not stream.send_pending(timeout=0.05)
    assert stream.failed == 0


def test_an_empty_frame_is_still_sent(stream, slot, server, state):
    """A detector running with nothing detected. The spec wants it sent."""
    slot.put(frame(1, state.snapshot().config_version, n=0))

    assert stream.send_pending()
    assert server.received("detection")[-1].body["delay"] == []


def test_the_newest_frame_wins(stream, slot, server, state):
    """While a POST is slow a newer frame replaces the pending one rather than
    queueing behind it."""
    for seq in range(1, 4):
        slot.put(frame(seq, state.snapshot().config_version))

    stream.send_pending()

    assert server.received("detection")[-1].body["seq"] == 3


# ── a frame is never retried ─────────────────────────────────────────


def test_a_failed_frame_is_dropped_not_retried(stream, slot, server, state):
    """By the time a retry could land there is a newer frame worth more, and
    the association window is 4 s — a late frame associates wrongly."""
    version = state.snapshot().config_version
    server.enqueue("detection", 503)
    slot.put(frame(1, version))

    assert not stream.send_pending()

    slot.put(frame(2, version))
    stream.send_pending()

    sent = [r.body["seq"] for r in server.received("detection")]
    assert sent == [1, 2]  # 1 was attempted once and abandoned


def test_rate_limiting_drops_rather_than_accumulates(stream, slot, server):
    """There is nothing to accumulate in, which is the point of the slot."""
    server.enqueue("detection", 429, retry_after=30)
    slot.put(frame(1))

    assert not stream.send_pending()
    assert stream.failed == 1


def test_a_conflict_queues_a_config_resend_and_drops_the_frame(stream, slot, state, server):
    """Re-sending after the configuration lands would put a stale timestamp on
    the wire."""
    state.config_resend.clear()
    slot.put(frame(1, config_version=99))

    assert not stream.send_pending()
    assert state.config_resend.is_set()
    assert len(server.received("detection")) == 1


def test_a_revoked_token_stops_the_stream(stream, slot, state, server):
    server.enqueue("detection", 401)
    slot.put(frame(1))

    stream.send_pending()

    assert state.snapshot().token_rejected
    assert not state.snapshot().may_stream


# ── gating ───────────────────────────────────────────────────────────


def test_a_paused_node_does_not_send(stream, slot, state, server):
    state.apply_levels(streaming_allowed=False)
    slot.put(frame(1))

    assert not stream.send_pending()
    assert server.received("detection") == []


def test_a_paused_node_still_drains_the_slot(stream, slot, state):
    """So resuming does not flush a frame from whenever it was paused."""
    state.apply_levels(streaming_allowed=False)
    slot.put(frame(1))
    stream.send_pending()

    state.apply_levels(streaming_allowed=True)

    assert not stream.send_pending(timeout=0.05)


def test_resuming_sends_the_current_frame(stream, slot, state, server):
    state.apply_levels(streaming_allowed=False)
    slot.put(frame(1))
    stream.send_pending()

    state.apply_levels(streaming_allowed=True)
    slot.put(frame(2))
    stream.send_pending()

    assert [r.body["seq"] for r in server.received("detection")] == [2]


def test_an_unregistered_node_does_not_send(server, slot, unregistered):
    stream = DetectionStream(Client(server.url), unregistered, slot)
    slot.put(frame(1))

    assert not stream.send_pending()
    assert server.received("detection") == []


# ── the ack ──────────────────────────────────────────────────────────


def test_an_accepted_mismatch_is_logged(stream, slot, server, state, caplog):
    """The spec says a mismatch against what was sent is worth logging."""
    server.enqueue(
        "detection", 202, body={"accepted": 0, "config_stale": False, "streaming_allowed": True}
    )
    slot.put(frame(1, state.snapshot().config_version, n=3))

    with caplog.at_level("WARNING"):
        stream.send_pending()

    assert "accepted 0 of 3" in caplog.text


def test_levels_on_the_ack_are_applied(stream, slot, state, server):
    server.enqueue(
        "detection",
        202,
        body={"accepted": 1, "config_stale": True, "streaming_allowed": False},
    )
    slot.put(frame(1))

    stream.send_pending()

    assert not state.snapshot().streaming_allowed
    assert state.config_resend.is_set()
