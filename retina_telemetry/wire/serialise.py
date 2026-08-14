"""Turning a payload model into the bytes that go on the wire.

There is one rule, and ``exclude_none=True`` cannot express it:

    **Drop a ``None`` only if the field is optional. Keep it if the spec
    requires it, even when its value is null.**

A required-nullable field's ``null`` is a *value the server is expecting*
rather than an absence. Dropping the key produces a payload it rejects.

This module existed once before, for a single field, and was deleted on
2026-08-11 when that field became optional and left the rule with no subject.
The v1.1.1 revision brings it back with six, and makes the pattern explicit
rather than incidental — from the spec's own "Saying I do not know":

    ``null`` on a field that is present means **known to be unknown**. Fields
    where that is a real state are required and nullable rather than optional,
    so there is exactly one way to express it and absence is not left carrying
    meaning.

So this is now load-bearing on every payload we send:

===============================  ==================================
``HeartbeatRequest``             ``config_version`` — no version issued yet
``NodeHealth``                   ``cpu_pct``, ``disk_free_mb``, ``temp_c``,
                                 ``blah2`` — read attempted, nothing to report
``NodeConfig``                   ``beam_width_deg``, ``beam_azimuth_deg`` —
                                 antenna not characterised
===============================  ==================================

The rule is derived from ``is_required()`` on the generated models rather than
written as a list of exceptions, so a revision that adds or removes one of
these needs no change here. ``tests/wire/test_payload_encoding.py`` asserts the
set above matches what the spec actually declares, so the two cannot drift
apart silently.

Note that ``adsb_hex``'s nullable *items* are a different thing entirely. They
live inside a list and nothing here touches them.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def to_wire(model: BaseModel) -> dict[str, Any]:
    """Serialise a payload, keeping required nulls and dropping optional ones.

    Returns:
        A JSON-ready dict — datetimes are RFC 3339 strings, not ``datetime``
        objects, because ``model_dump(mode="json")`` does the encoding. That
        argument is as load-bearing as the pruning: without it ``json.dumps``
        refuses the registration payload outright.
    """
    return _prune(model, model.model_dump(mode="json"))


def to_wire_json(model: BaseModel, **kwargs: Any) -> str:
    """:func:`to_wire`, as a JSON string."""
    return json.dumps(to_wire(model), **kwargs)


def _prune(model: BaseModel, encoded: dict[str, Any]) -> dict[str, Any]:
    pruned: dict[str, Any] = {}
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        if value is None and not field.is_required():
            continue
        pruned[name] = (
            _prune(value, encoded[name]) if isinstance(value, BaseModel) else encoded[name]
        )
    return pruned
