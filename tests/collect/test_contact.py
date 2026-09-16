"""Reading the contact document retina-gui writes.

The interesting half is absence. Every field is optional and so is the whole
document, so most of what this reader has to get right is the many ways a node
legitimately has nothing to say.
"""

from __future__ import annotations

import json

from retina_telemetry.collect.contact import Contact, read_contact

FULL = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+441234567890",
    "country": "GB",
}


def write(tmp_path, document):
    path = tmp_path / "telemetry-contact.json"
    path.write_text(json.dumps(document))
    return path


def test_a_full_document_is_read_field_for_field(tmp_path):
    contact = read_contact(write(tmp_path, FULL))

    assert contact == Contact(**FULL)
    assert contact.is_empty is False


def test_a_partial_document_is_normal(tmp_path):
    """Each field is independently optional, so an owner who gave only an
    email is a complete answer rather than a half-filled form."""
    contact = read_contact(write(tmp_path, {"email": "ada@example.com"}))

    assert contact.email == "ada@example.com"
    assert contact.first_name is None
    assert contact.is_empty is False


# ── absence, in all the ways it arrives ──────────────────────────────


def test_an_absent_file_is_empty_rather_than_an_error(tmp_path):
    """The ordinary state of a node whose owner skipped the step. Nothing
    about it may stop the node registering or streaming."""
    contact = read_contact(tmp_path / "nothing-here.json")

    assert contact == Contact()
    assert contact.is_empty is True


def test_an_empty_object_is_empty(tmp_path):
    assert read_contact(write(tmp_path, {})).is_empty is True


def test_explicit_nulls_are_empty(tmp_path):
    assert read_contact(write(tmp_path, dict.fromkeys(FULL, None))).is_empty is True


def test_blank_strings_are_not_details(tmp_path):
    """A space in the email field would otherwise be sent to the server and
    shown back to the owner as though it were something."""
    contact = read_contact(write(tmp_path, {**FULL, "email": "   ", "phone": ""}))

    assert contact.email is None
    assert contact.phone is None
    assert contact.first_name == "Ada"


def test_surrounding_whitespace_is_trimmed(tmp_path):
    assert read_contact(write(tmp_path, {"first_name": "  Ada  "})).first_name == "Ada"


# ── a malformed file never costs the node anything ───────────────────


def test_unparseable_json_reads_as_empty(tmp_path):
    """Never raises. An unreadable contact document is indistinguishable in
    consequence from an absent one, and neither is a reason to stop."""
    path = tmp_path / "telemetry-contact.json"
    path.write_text("{not json")

    assert read_contact(path).is_empty is True


def test_a_document_that_is_not_an_object_reads_as_empty(tmp_path):
    assert read_contact(write(tmp_path, ["Ada"])).is_empty is True


def test_a_non_string_value_is_dropped_rather_than_coerced(tmp_path):
    """`str(12345)` would turn a hand-edited number into a phone number the
    owner never typed."""
    contact = read_contact(write(tmp_path, {"phone": 441234567890, "first_name": "Ada"}))

    assert contact.phone is None
    assert contact.first_name == "Ada"


def test_unknown_fields_are_ignored(tmp_path):
    """`NodeContact` forbids extra properties, so carrying one through would
    have the server refuse the whole document over a field nobody reads."""
    contact = read_contact(write(tmp_path, {**FULL, "twitter": "@ada"}))

    assert contact == Contact(**FULL)


# ── change detection ─────────────────────────────────────────────────


def test_equality_is_the_change_check(tmp_path):
    """Sent on local change only, so comparing the whole document is what
    decides there was one. Same mechanism as NodeConfigRaw."""
    first = read_contact(write(tmp_path, FULL))
    again = read_contact(write(tmp_path, FULL))

    assert first == again
    assert first != read_contact(write(tmp_path, {**FULL, "phone": "+15551234567"}))


def test_clearing_is_a_change_not_a_disappearance(tmp_path):
    """An owner who deletes their details produces an empty document, which
    differs from what was sent and must therefore travel."""
    before = read_contact(write(tmp_path, FULL))
    after = read_contact(write(tmp_path, {}))

    assert after != before
    assert after.is_empty is True
