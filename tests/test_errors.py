import threading

from retina_telemetry.errors import Errors


def test_a_fault_is_recorded():
    errors = Errors()
    errors.add("detection poll failed: connection refused")

    assert errors.snapshot() == ["detection poll failed: connection refused"]


def test_repeats_are_counted_not_appended():
    """A wedged node produces a fault per poll. 120 identical strings say
    nothing that one and a count do not."""
    errors = Errors()
    for _ in range(120):
        errors.add("detection poll failed: connection refused")

    assert errors.snapshot() == ["detection poll failed: connection refused (x120)"]


def test_distinct_faults_are_all_kept():
    errors = Errors()
    errors.add("blah2 unreachable")
    errors.add("config unreadable")

    assert len(errors.snapshot()) == 2


def test_the_most_frequent_fault_comes_first():
    errors = Errors()
    errors.add("rare")
    for _ in range(5):
        errors.add("common")

    assert errors.snapshot()[0].startswith("common")


def test_the_list_is_bounded():
    """The beat is every 60 s; an unbounded list would make the payload
    useless for the thing it exists to convey."""
    errors = Errors(limit=3)
    for i in range(10):
        errors.add(f"fault {i}")

    assert len(errors.snapshot()) == 3


def test_a_repeated_fault_survives_eviction():
    """Dropping the least frequent rather than the oldest: a fault seen once
    is likelier to be noise than one seen repeatedly."""
    errors = Errors(limit=2)
    for _ in range(5):
        errors.add("persistent")
    errors.add("noise one")
    errors.add("noise two")

    assert any("persistent" in entry for entry in errors.snapshot())


def test_draining_clears():
    errors = Errors()
    errors.add("blah2 unreachable")

    drained = errors.drain()

    assert drained == ["blah2 unreachable"]
    assert errors.snapshot() == []


def test_snapshot_does_not_clear():
    """The status document reads without consuming; only a heartbeat drains."""
    errors = Errors()
    errors.add("blah2 unreachable")

    errors.snapshot()

    assert errors.snapshot() == ["blah2 unreachable"]


def test_empty_messages_are_ignored():
    errors = Errors()
    errors.add("")

    assert errors.snapshot() == []


def test_concurrent_adds_are_all_counted():
    errors = Errors(limit=100)

    def add():
        for _ in range(200):
            errors.add("shared fault")

    threads = [threading.Thread(target=add) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors.snapshot() == ["shared fault (x1600)"]
