"""The only way a failure reaches the operator.

This service binds no ports, so there is no endpoint to ask. Three conditions
have to reach a human and none of them will otherwise: no identity, a revoked
token, and a configuration the server rejected. A log line inside a container
is not a route to anyone.

It is not only for failures, though. ``node_ref`` is the node's public
identifier, and the spec says the node shows it to the owner so they can find
their data on the map. It arrives only in a registration or heartbeat response,
which only this service ever sees, and it can rotate without warning. So this
document is the sole path by which an owner ever learns it.

## The contract with retina-gui

Written here, read there. Deliberately mirrors the shape retina-gui already
handles for ``mender-update.status`` — a JSON object carrying its own timestamp,
treated as stale past a timeout — so it needs no new pattern on that side.

* **Mode 0644, not 0600.** Easy to get wrong by copying the token's handling.
  A different container reads this, and an unreadable status document is a
  pointless one. It never contains the token; there is a test.
* **``schema`` is an integer.** retina-node pins every image separately, so this
  service and retina-gui being at different versions is the normal case rather
  than an exception. This is a cross-version contract and is versioned like one.
* **``written_at`` on every write**, so a reader can tell "idle" from "dead".
  A stale file means this service is not running; an absent one means it never
  has.
* **Atomic**, or retina-gui eventually reads a half-written file and throws
  mid-render.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from retina_telemetry.state import Snapshot

log = logging.getLogger(__name__)

DEFAULT_STATUS_PATH = Path("/data/retina-telemetry/status.json")

#: World-readable on purpose: retina-gui runs as a different user and cannot
#: read a 0600 file. Nothing secret goes in here.
STATUS_MODE = 0o644

#: Bumped only when a reader would have to change. Adding a field does not
#: require it; removing or repurposing one does.
SCHEMA = 1


class StatusWriter:
    """Writes the status document. One instance, called from one thread.

    Kept to a single writer so there is no locking here: every value it needs
    already comes from something that is itself thread-safe.
    """

    def __init__(self, path: Path | str = DEFAULT_STATUS_PATH) -> None:
        self._path = Path(path)

    def write(
        self,
        *,
        state: str,
        snapshot: Snapshot,
        node_id: str | None = None,
        detail: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        """Write the current state.

        Args:
            state: the node's own account of itself, from the lifecycle. The
                same vocabulary the heartbeat uses, plus the values that never
                reach the server because they describe a node that cannot send.
            snapshot: from ``State.snapshot()``. Only its redacted view is used,
                so the token cannot be written by accident.
            node_id: shown so an operator can identify the node without
                consulting Mender. ``None`` when it could not be read, which is
                itself the most important thing this document ever says.
            detail: why, in a sentence, when ``state`` alone is not enough.
            errors: the bounded accumulator's current contents.
        """
        document: dict[str, Any] = {
            "schema": SCHEMA,
            "written_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "state": state,
            "detail": detail,
            "node_id": node_id,
            "errors": errors or [],
            **snapshot.redacted(),
        }

        self._write_atomically(json.dumps(document, indent=2, sort_keys=True))

    def _write_atomically(self, payload: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(self._path.name + ".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, STATUS_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            os.replace(temporary, self._path)
            os.chmod(self._path, STATUS_MODE)
        except OSError as exc:
            # Never fatal, and deliberately not retried. The uplink is the job;
            # this describes the job. Failing to describe it must not stop it.
            log.error("cannot write %s: %s", self._path, exc)
