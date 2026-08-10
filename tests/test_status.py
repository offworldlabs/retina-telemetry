import json
import stat

import pytest

from retina_telemetry.state import State
from retina_telemetry.status import SCHEMA, StatusWriter


@pytest.fixture
def path(tmp_path):
    return tmp_path / "retina-telemetry" / "status.json"


@pytest.fixture
def state(tmp_path):
    state = State(tmp_path / "token")
    state.store_token("tok_secret123", node_ref="nd4f2k9xq7m3b8vc", config_version=7)
    return state


def written(path):
    return json.loads(path.read_text())


def test_the_document_is_written(path, state):
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert written(path)["state"] == "streaming"


def test_it_carries_a_schema_version(path, state):
    """retina-node pins every image separately, so this service and retina-gui
    being at different versions is normal rather than exceptional."""
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert written(path)["schema"] == SCHEMA


def test_it_carries_a_timestamp(path, state):
    """So a reader can tell 'idle' from 'dead'."""
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert written(path)["written_at"].endswith("Z")


def test_it_is_readable_by_another_container(path, state):
    """0644, not 0600. retina-gui runs as a different user and an unreadable
    status document is a pointless one."""
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_the_token_is_never_written(path, state):
    """The one file that crosses a trust boundary outward."""
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert "tok_secret123" not in path.read_text()


def test_node_ref_reaches_the_owner(path, state):
    """The spec says the node shows this to the owner so they can find their
    data on the map, and this document is the only path it takes."""
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert written(path)["node_ref"] == "nd4f2k9xq7m3b8vc"


def test_a_missing_identity_is_the_headline(path, tmp_path):
    """One of the three conditions that must reach a human."""
    fresh = State(tmp_path / "absent-token")

    StatusWriter(path).write(
        state="no_identity",
        snapshot=fresh.snapshot(),
        node_id=None,
        detail="/data/mender/node_id is missing or unreadable",
    )

    document = written(path)
    assert document["state"] == "no_identity"
    assert document["node_id"] is None
    assert "missing" in document["detail"]


def test_errors_are_included(path, state):
    StatusWriter(path).write(
        state="streaming",
        snapshot=state.snapshot(),
        errors=["detection poll failed: connection refused (x12)"],
    )

    assert written(path)["errors"] == ["detection poll failed: connection refused (x12)"]


def test_errors_default_to_empty(path, state):
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert written(path)["errors"] == []


def test_rewriting_replaces_rather_than_appends(path, state):
    writer = StatusWriter(path)
    writer.write(state="registering", snapshot=state.snapshot())

    writer.write(state="streaming", snapshot=state.snapshot())

    assert written(path)["state"] == "streaming"


def test_no_temporary_file_is_left_behind(path, state):
    """Atomic, or retina-gui eventually reads a half-written file."""
    StatusWriter(path).write(state="streaming", snapshot=state.snapshot())

    assert list(path.parent.iterdir()) == [path]


def test_an_unwritable_path_does_not_crash_the_node(tmp_path, state):
    """The uplink is the job; this describes the job. Failing to describe it
    must not stop it."""
    blocked = tmp_path / "nope"
    blocked.mkdir()
    blocked.chmod(0o500)

    try:
        StatusWriter(blocked / "deeper" / "status.json").write(
            state="streaming", snapshot=state.snapshot()
        )
    finally:
        blocked.chmod(0o700)
