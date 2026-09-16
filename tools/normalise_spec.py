#!/usr/bin/env python3
"""Rewrite the spec's nullable spelling into the one the generator understands.

**This does not change the contract.** It reads the spec, rewrites one JSON
Schema idiom into an exactly equivalent one, and writes the result somewhere
else. ``docs/node-ingest-v1.yml`` is never touched: it stays byte-identical to
what the server author sent, which is the whole point of keeping it read-only.

## The idiom

OpenAPI 3.1 has two ways to say "a number between -90 and 90, or null"::

    anyOf:                          type: [number, "null"]
    - type: number                  minimum: -90
      minimum: -90                  maximum: 90
      maximum: 90
    - type: 'null'

They mean the same thing. The 1.1.1 spec was hand-written and used the second;
1.2.0 is generated from the server's own FastAPI models and uses the first, so
every revision from here on will.

## Why it matters

``datamodel-codegen`` handles the two very differently. Given a type array it
emits what we want::

    rx_lat: Annotated[float | None, Field(ge=-90.0, le=90.0)]

Given ``anyOf`` it cannot attach the branch's constraints to a union member, so
it manufactures a wrapper type per field::

    class RxLat(RootModel[float]):
        root: Annotated[float, Field(ge=-90.0, le=90.0)]

    rx_lat: RxLat | None

``--collapse-root-models`` does not reach these, because they are synthesised
from an inline branch rather than declared as a ``$ref``. The consequence is
``RxLat(root=51.4)`` at construction and ``.root`` at every read, spreading a
generation artefact through code that should only ever see floats. Adopting
1.2.0 raised the count from 3 wrappers to 19.

So the rewrite happens here, on a temporary copy, and the generated models keep
the shape every call site already expects.

## What is left alone

Only a two-member ``anyOf`` where one member is exactly ``{"type": "null"}`` and
the other declares a ``type``. A nullable ``$ref`` (``HeartbeatRequest.health``)
has no type to hoist and already generates correctly as ``NodeHealth | None``,
so it passes through untouched. Anything else (a three-way union, a ``oneOf``,
a bare ``anyOf`` with no null member) is not this idiom and is left
exactly as written.
"""

from __future__ import annotations

import sys
from typing import Any

import yaml

NULL_BRANCH = {"type": "null"}


def normalise(node: Any) -> Any:
    """Depth-first rewrite of the nullable-``anyOf`` idiom.

    Siblings of the ``anyOf`` win over the branch's own keys on a collision, so
    a ``title`` or ``description`` written beside the union is the one that
    survives. In practice nothing collides except those two.
    """
    if isinstance(node, list):
        return [normalise(item) for item in node]
    if not isinstance(node, dict):
        return node

    node = {key: normalise(value) for key, value in node.items()}
    branch = _nullable_branch(node.get("anyOf"))
    if branch is None:
        return node

    merged = {key: value for key, value in node.items() if key != "anyOf"}
    for key, value in branch.items():
        if key == "type":
            merged["type"] = [value, "null"]
        else:
            merged.setdefault(key, value)
    return merged


def _nullable_branch(any_of: Any) -> dict[str, Any] | None:
    """The non-null member, if this is ``anyOf: [<something typed>, null]``."""
    if not isinstance(any_of, list) or len(any_of) != 2:
        return None
    others = [member for member in any_of if member != NULL_BRANCH]
    if len(others) != 1 or len(any_of) - len(others) != 1:
        return None
    # A `$ref` branch carries no `type` to hoist into the array, and generates
    # correctly as it stands.
    return others[0] if isinstance(others[0], dict) and "type" in others[0] else None


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <spec.yml> <output.yml>", file=sys.stderr)
        return 2

    source, target = sys.argv[1], sys.argv[2]
    with open(source, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)

    with open(target, "w", encoding="utf-8") as handle:
        yaml.safe_dump(normalise(document), handle, sort_keys=False, allow_unicode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
