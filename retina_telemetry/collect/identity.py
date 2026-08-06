"""Node identity — read from disk, never derived.

``/data/mender/node_id`` is written by owl-os's Mender identity script on first
boot and returned unconditionally thereafter. If it is absent, the node has
either not enrolled with Mender or has fallen back to a ``mac=`` identity. Both
are hard local errors: a node that invents its own identity causes far more
trouble than one that will not start, because a MAC-derived value changes with
a network hardware swap and presents to the server as an entirely new node.

Two landmines this module exists to avoid:

* ``retina-gui``'s ``get_node_id()`` returns the string ``'Unknown'`` when the
  file is missing. It is display-only there. Do not import or imitate it.
* ``retina-node/config/default.yml`` carries ``network.node_id: "ret000000000"``
  — a static placeholder, identical on every node, and twelve characters, so it
  fails the spec's pattern. Nothing here reads node config.

So this module raises rather than returning anything. What to *do* about that
is the lifecycle's decision, not collection's.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_NODE_ID_PATH = Path("/data/mender/node_id")

#: ``ret`` plus eight lowercase hex characters, per the ingest spec's ``NodeId``.
NODE_ID_PATTERN = re.compile(r"^ret[0-9a-f]{8}$")


class IdentityUnavailable(Exception):
    """The node has no usable identity. Never resolved by substituting one."""


def read_node_id(path: Path | str = DEFAULT_NODE_ID_PATH) -> str:
    """Read and validate the node's identity.

    Returns:
        The ``ret……`` identifier, exactly as stored.

    Raises:
        IdentityUnavailable: if the file is missing, unreadable, empty, or does
            not match the spec's pattern. There is no fallback and no default.
    """
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise IdentityUnavailable(
            f"{path} does not exist — the node has not enrolled with Mender"
        ) from exc
    except OSError as exc:
        raise IdentityUnavailable(f"{path} could not be read: {exc}") from exc

    node_id = raw.strip()
    if not node_id:
        raise IdentityUnavailable(f"{path} is empty")

    if not NODE_ID_PATTERN.match(node_id):
        raise IdentityUnavailable(
            f"{path} contains {node_id!r}, which is not a valid node_id "
            f"(expected {NODE_ID_PATTERN.pattern})"
        )

    return node_id
