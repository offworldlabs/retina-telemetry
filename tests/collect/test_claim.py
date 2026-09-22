"""Reading the claim document retina-gui writes.

Two keys that mean different kinds of thing: an address, which is state, and a
timestamp, which is an event. Most of what matters here is that a file nobody
wrote, or wrote badly, produces a node nobody claims rather than an exception,
because an unclaimed node is a perfectly ordinary one.
"""

import json

from retina_telemetry.collect.claim import Nomination, read_nomination

ADDRESS = "owner@example.com"


def write(tmp_path, document):
    path = tmp_path / "telemetry-claim.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_an_address_and_an_ask_are_read(tmp_path):
    path = write(tmp_path, {"email": ADDRESS, "send_requested_at": "2026-09-22T11:30:00Z"})

    nomination = read_nomination(path)

    assert nomination.email == ADDRESS
    assert nomination.send_requested_at.isoformat() == "2026-09-22T11:30:00+00:00"


def test_an_absent_file_is_a_node_nobody_has_claimed(tmp_path):
    """The ordinary state, not a gap: an unclaimed node records and reports
    exactly as a claimed one does."""
    assert read_nomination(tmp_path / "nothing.json") == Nomination()


def test_an_address_with_no_ask_is_normal(tmp_path):
    """What the file holds until somebody presses send again."""
    path = write(tmp_path, {"email": ADDRESS})

    assert read_nomination(path) == Nomination(email=ADDRESS, send_requested_at=None)


def test_surrounding_whitespace_is_dropped(tmp_path):
    path = write(tmp_path, {"email": f"  {ADDRESS}\n"})

    assert read_nomination(path).email == ADDRESS


def test_the_address_is_not_lower_cased_here(tmp_path):
    """The server states that it trims and lower cases before judging, so
    doing it here as well would be a second implementation of somebody else's
    rule, free to disagree with it."""
    path = write(tmp_path, {"email": "Owner@Example.COM"})

    assert read_nomination(path).email == "Owner@Example.COM"


def test_a_non_string_address_is_dropped_rather_than_coerced(tmp_path):
    """`str(12345)` would turn a hand-edited number into an address, and this
    one gets mailed."""
    path = write(tmp_path, {"email": 12345})

    assert read_nomination(path).email is None


def test_an_unparseable_ask_is_dropped(tmp_path):
    """Not treated as "now", which would mail somebody because a file was
    edited badly. Dropping it costs one press of a button instead."""
    path = write(tmp_path, {"email": ADDRESS, "send_requested_at": "last Tuesday"})

    nomination = read_nomination(path)

    assert nomination.email == ADDRESS
    assert nomination.send_requested_at is None


def test_malformed_json_reads_as_unclaimed(tmp_path):
    path = tmp_path / "telemetry-claim.json"
    path.write_text("{not json", encoding="utf-8")

    assert read_nomination(path) == Nomination()


def test_a_document_that_is_not_an_object_reads_as_unclaimed(tmp_path):
    path = write(tmp_path, [ADDRESS])

    assert read_nomination(path) == Nomination()


def test_unknown_fields_are_ignored(tmp_path, caplog):
    """`NodeClaimRequest` forbids extra properties, so passing one on would
    have the server refuse the lot."""
    path = write(tmp_path, {"email": ADDRESS, "nickname": "the shed"})

    with caplog.at_level("WARNING"):
        nomination = read_nomination(path)

    assert nomination.email == ADDRESS
    assert "nickname" in caplog.text


def test_the_address_is_never_in_a_log_line(tmp_path, caplog):
    """Same rule the contact reader follows: field names, never values."""
    path = write(tmp_path, {"email": ADDRESS, "nickname": "the shed"})

    with caplog.at_level("WARNING"):
        read_nomination(path)

    assert ADDRESS not in caplog.text
