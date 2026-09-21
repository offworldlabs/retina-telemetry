"""The node_id format, checked against the canonical vectors.

The shape of a node_id is hand-copied into four repositories in two languages,
which is exactly how ``owl-mdns-identity`` came to derive a different id from
the script that actually writes the file. A shared library across four repos is
not realistic; a shared *artefact* is, so each repo vendors the same vectors and
checks itself against them rather than against the others.

``tests/node-id-test-vectors.json`` is a verbatim copy of
``owl-os/configuration/mender/identity/node-id-test-vectors.json``. owl-os owns
the generator, so it owns the vectors. Re-copy the file rather than editing this
one if they ever disagree.
"""

import json
import pathlib

import pytest

from retina_telemetry.collect.identity import NODE_ID_PATTERN

VECTORS = json.loads((pathlib.Path(__file__).parent / "node-id-test-vectors.json").read_text())


def test_our_pattern_is_the_canonical_one():
    """A vendored copy that has drifted is worse than no copy, because it still
    looks authoritative."""
    assert NODE_ID_PATTERN.pattern == VECTORS["pattern"]


@pytest.mark.parametrize("case", VECTORS["valid"], ids=lambda c: c["node_id"])
def test_valid_ids_match(case):
    assert NODE_ID_PATTERN.match(case["node_id"]), case["why"]


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda c: c["node_id"])
def test_invalid_ids_do_not_match(case):
    assert not NODE_ID_PATTERN.match(case["node_id"]), case["why"]


@pytest.mark.parametrize("case", VECTORS["derive"], ids=lambda c: c["serial"])
def test_derived_ids_are_well_formed(case):
    """This repo never derives an id — that is owl-os's job, and duplicating it
    is the mistake this file exists to catch. What it must do is recognise
    every id owl-os can produce."""
    assert NODE_ID_PATTERN.match(case["node_id"]), case["serial"]
