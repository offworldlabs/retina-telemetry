"""The two rewrites that stand between the contract and the generated models.

Neither changes what the spec means. Both change what `datamodel-codegen`
builds from it, and in ways that reach every call site, so they are worth
holding still.
"""

from pathlib import Path

import yaml

from tools.normalise_spec import name_enums, normalise

SPEC = Path(__file__).resolve().parents[1] / "docs" / "node-ingest-v1.yml"

TRACK_STATE_VALUES = ["active", "coasting", "deleted"]
CLAIM_VALUES = ["unclaimed", "pending", "owned"]
NODE_STATE_VALUES = ["starting", "streaming", "stalled", "paused", "error", "stopping"]


def spec() -> dict:
    return yaml.safe_load(SPEC.read_text(encoding="utf-8"))


def inline_enums(node, found=None) -> list[tuple[str | None, tuple[str, ...]]]:
    """Every enum still declared inline, as (title, values)."""
    found = [] if found is None else found
    if isinstance(node, list):
        for item in node:
            inline_enums(item, found)
    elif isinstance(node, dict):
        values = node.get("enum")
        if isinstance(values, list):
            found.append((node.get("title"), tuple(values)))
        for value in node.values():
            inline_enums(value, found)
    return found


# ── nullable fields ──────────────────────────────────────────────────


def test_a_nullable_anyOf_becomes_a_type_array():
    """The whole reason this module exists: `anyOf` makes the generator
    manufacture a RootModel wrapper per field, a type array does not."""
    rewritten = normalise({"anyOf": [{"type": "number", "minimum": -90}, {"type": "null"}]})

    assert rewritten == {"type": ["number", "null"], "minimum": -90}


def test_a_nullable_ref_is_left_alone():
    """`HeartbeatRequest.health`. No type to hoist, and it already generates
    correctly as `NodeHealth | None`."""
    union = {"anyOf": [{"$ref": "#/components/schemas/NodeHealth"}, {"type": "null"}]}

    assert normalise(union) == union


def test_a_union_that_is_not_the_idiom_is_left_alone():
    union = {"anyOf": [{"type": "string"}, {"type": "integer"}]}

    assert normalise(union) == union


# ── enums that would collide ─────────────────────────────────────────


def test_a_named_enum_is_hoisted_to_a_component():
    document = name_enums({"properties": {"state": {"type": "string", "enum": CLAIM_VALUES}}})

    assert document["properties"]["state"] == {"$ref": "#/components/schemas/ClaimState"}
    assert document["components"]["schemas"]["ClaimState"]["enum"] == CLAIM_VALUES


def test_every_occurrence_refs_the_one_component():
    """Three fields spell this enum in 1.4.0. One class, not three."""
    document = name_enums(
        {
            "a": {"type": "string", "enum": CLAIM_VALUES, "title": "State"},
            "b": {"type": "string", "enum": CLAIM_VALUES, "title": "Claim State"},
            "c": {"type": "string", "enum": CLAIM_VALUES, "title": "Claim State"},
        }
    )

    refs = {document[key]["$ref"] for key in "abc"}
    assert refs == {"#/components/schemas/ClaimState"}
    assert list(document["components"]["schemas"]) == ["ClaimState"]


def test_the_hoisted_component_is_not_rewritten_into_a_ref_to_itself():
    document = name_enums({"components": {"schemas": {}}, "x": {"enum": CLAIM_VALUES}})

    assert document["components"]["schemas"]["ClaimState"]["enum"] == CLAIM_VALUES


def test_an_enum_we_do_not_name_is_untouched():
    """The node's own state stays exactly as the contract declares it."""
    node_state = {"type": "string", "enum": NODE_STATE_VALUES, "title": "State"}

    assert name_enums({"state": node_state})["state"] == node_state


# ── against the real contract ────────────────────────────────────────


def test_the_node_state_enum_keeps_its_name():
    """The regression this was written for.

    `ClaimResponse.state` is titled `State`, exactly like `HeartbeatRequest.state`.
    The generator names enum classes after that title and renames the loser
    `State1`, so without the hoist the node state becomes `State1` while
    `comms/lifecycle.py` and `wire/heartbeat.py` go on importing `State` and
    silently get the claim enum instead.
    """
    document = name_enums(normalise(spec()))
    beat = document["components"]["schemas"]["HeartbeatRequest"]["properties"]["state"]

    assert beat["title"] == "State"
    assert beat["enum"] == NODE_STATE_VALUES


def test_no_two_inline_enums_share_a_title():
    """The general form, so the next collision fails here rather than in a
    rename nobody reads. A revision that introduces one adds a line to
    NAMED_ENUMS."""
    by_title: dict[str | None, set[tuple[str, ...]]] = {}
    for title, values in inline_enums(name_enums(normalise(spec()))):
        by_title.setdefault(title, set()).add(values)

    collisions = {title: shapes for title, shapes in by_title.items() if len(shapes) > 1}
    assert collisions == {}


def test_only_the_named_enums_are_hoisted():
    """Keeps the table honest: an entry that stops matching the contract shows
    up as a component nothing refs."""
    document = name_enums(normalise(spec()))
    schemas = document["components"]["schemas"]

    assert schemas["ClaimState"]["enum"] == CLAIM_VALUES
    assert schemas["TrackState"]["enum"] == TRACK_STATE_VALUES
    assert sorted(name for name in schemas if name.endswith("State")) == [
        "ClaimState",
        "TrackState",
    ]
