#!/usr/bin/env python3
"""Rewrite two spec idioms into the spellings the generator understands.

**This does not change the contract.** It reads the spec, rewrites two JSON
Schema idioms into exactly equivalent ones, and writes the result somewhere
else. ``docs/node-ingest-v1.yml`` is never touched: it stays byte-identical to
what the server author sent, which is the whole point of keeping it read-only.

Both rewrites exist for the same reason: ``datamodel-codegen`` produces a
different *shape* for two spellings that mean the same thing, and the shape we
want is not the one the server's FastAPI export happens to emit.

# 1. Nullable fields

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

# 2. Inline enums that collide on their title

## The idiom

The contract declares enums inline on the property rather than as named
components, and carries the name in ``title``::

    state:                          claim_state:
      type: string                    type: string
      enum: [starting, streaming,     enum: [unclaimed, pending, owned]
             stalled, paused,         title: Claim State
             error, stopping]
      title: State

## Why it matters

``datamodel-codegen`` names a generated enum class after that title, and two
unrelated enums in 1.4.0 both answer to ``State``: the node's own six-value
state on ``HeartbeatRequest``, and where a claim stands on ``ClaimResponse``.
1.6.0 added a third, where a track stands on ``Track``.
Faced with the collision the generator keeps the first it meets and renames the
second ``State1``.

Which one loses is a function of declaration order in someone else's file. In
1.4.0 it is the node state that becomes ``State1``, and the damage is silent:
``comms/lifecycle.py`` and ``wire/heartbeat.py`` both do ``import State as
WireState``, so they would go on importing a name that still exists and now
means something else entirely. The first sign would be ``WireState.streaming``
raising ``AttributeError`` while building a heartbeat.

A positional name cannot be depended on either way, so the fix is to stop the
collision happening rather than to chase the number.

## What is rewritten

Each enum listed in ``NAMED_ENUMS`` is hoisted into a component schema of that
name and every inline occurrence replaced by a ``$ref`` to it. The generator
then emits one class, under a name chosen here, shared by every field that
refs it, which is also what the three claim-state fields should have been all
along, since they are the same three values in all three places.

Nothing else is touched. An enum not in the table generates exactly as before.
"""

from __future__ import annotations

import sys
from typing import Any

import yaml

NULL_BRANCH = {"type": "null"}

#: Enums to hoist out of the properties that declare them, and the name each
#: one gets. Keyed by the values because the values are what identify an enum:
#: the titles are exactly what cannot be trusted here, and the three
#: claim-state fields do not agree on one anyway (``State`` on
#: ``ClaimResponse.state``, ``Claim State`` on the other two).
#:
#: The first entry arrived when 1.4.0 introduced a second enum titled
#: ``State``, and the second when 1.6.0 introduced a third, on ``Track.state``.
#: A revision that collides again adds a line here rather than renaming
#: whatever the generator happened to demote that time.
NAMED_ENUMS: dict[tuple[str, ...], str] = {
    ("unclaimed", "pending", "owned"): "ClaimState",
    ("active", "coasting", "deleted"): "TrackState",
}


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


def name_enums(document: Any) -> Any:
    """Hoist every enum in ``NAMED_ENUMS`` into a component of that name.

    Runs over the whole document and replaces each inline occurrence with a
    ``$ref``, then adds the components themselves. Adding them afterwards is
    what stops the hoisted copy being rewritten into a reference to itself.
    """
    hoisted: dict[str, dict[str, Any]] = {}

    def rewrite(node: Any) -> Any:
        if isinstance(node, list):
            return [rewrite(item) for item in node]
        if not isinstance(node, dict):
            return node

        name = _named_enum(node)
        if name is None:
            return {key: rewrite(value) for key, value in node.items()}

        # Everything but the title, which is the one key the occurrences
        # disagree on and the one being replaced. Anything else they carry
        # (a description, say) comes along from the first occurrence seen;
        # today they carry nothing else.
        hoisted.setdefault(name, {**{k: v for k, v in node.items() if k != "title"}, "title": name})
        return {"$ref": f"#/components/schemas/{name}"}

    document = rewrite(document)
    if hoisted:
        schemas = document.setdefault("components", {}).setdefault("schemas", {})
        schemas.update(hoisted)
    return document


def _named_enum(node: dict[str, Any]) -> str | None:
    """The name this node should be hoisted under, if it is one we name."""
    values = node.get("enum")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        return None
    return NAMED_ENUMS.get(tuple(values))


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <spec.yml> <output.yml>", file=sys.stderr)
        return 2

    source, target = sys.argv[1], sys.argv[2]
    with open(source, encoding="utf-8") as handle:
        document = yaml.safe_load(handle)

    with open(target, "w", encoding="utf-8") as handle:
        rewritten = name_enums(normalise(document))
        yaml.safe_dump(rewritten, handle, sort_keys=False, allow_unicode=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
