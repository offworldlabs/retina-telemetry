"""Shared fixtures for the stage 3 tests.

Everything here runs against ``tools/mock_server.py`` over real HTTP. The
important consequence is that ``state`` holds a token the mock actually issued
— storing one locally that the server never minted would make every request
401, and the tests would be measuring the fixture rather than the code.
"""

import pytest

from retina_telemetry.comms.client import Client
from retina_telemetry.state import State
from retina_telemetry.wire.config import build_node_config
from tests.wire.test_config import OWL
from tools.mock_server import MockServer

REGISTRATION = {
    "node_id": "ret824685c9",
    "board_model": "pi5-v3-arm64",
    "agreements": {
        "licence": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
        "remote_management": {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"},
        "publication": {
            "version": "2026-07-01",
            "accepted_at": "2026-07-31T09:12:00Z",
            "choice": "public",
        },
    },
    "config": build_node_config(OWL).model_dump(mode="json", exclude_none=True),
}

CONFIG = build_node_config(OWL).model_dump(mode="json", exclude_none=True)


@pytest.fixture
def server():
    with MockServer() as running:
        yield running


@pytest.fixture
def unregistered(tmp_path):
    """A node that has never registered — where every node starts."""
    return State(tmp_path / "token")


@pytest.fixture
def state(tmp_path, server):
    """A registered node, holding a token the mock issued."""
    state = State(tmp_path / "token")
    outcome = Client(server.url).post("/nodes/register", REGISTRATION)
    assert outcome.ok, f"fixture could not register: {outcome.describe()}"

    body = outcome.body
    state.store_token(
        body["token"],
        node_ref=body["node_ref"],
        config_version=body["config_version"],
    )
    state.config_resend.clear()
    return state
