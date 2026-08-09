"""Turning a payload model into the bytes that go on the wire.

There is one rule and it cannot be expressed with pydantic's ``exclude_none``:

    **Drop a ``None`` only if the field is optional. Keep it if the spec
    requires it, even when its value is null.**

``exclude_none=True`` looks like the obvious choice — most of the ``None``s in
a heartbeat are fields we genuinely do not know and should omit. But
``NodeConfig.beam_azimuth_deg`` is *required* and *nullable*
(``type: [number, "null"]``, and listed under ``required``), because ``null``
is how the spec spells "broadside/omnidirectional". Dropping it produces a
payload the server rejects, and it is nested inside ``RegisterRequest``, so the
mistake propagates.

It is the only field in the spec with that combination — the other nullable
declaration is ``adsb_hex``'s *items*, which live inside a list and are
untouched by any of this. One field is exactly the number that gets missed,
which is why this is derived from the models rather than written as a list of
exceptions: ``is_required()`` comes from the generated models, which come from
the spec, so a future revision cannot silently invalidate it.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def to_wire(model: BaseModel) -> dict[str, Any]:
    """Serialise a payload, keeping required nulls and dropping optional ones.

    Returns:
        A JSON-ready dict — datetimes are RFC 3339 strings, not ``datetime``
        objects, because ``model_dump(mode="json")`` does the encoding.
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
