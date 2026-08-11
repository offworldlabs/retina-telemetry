import copy
import json

from retina_telemetry.collect.consent import (
    NONE_GIVEN,
    AcceptanceRecord,
    PublicationChoice,
    read_consent,
)
from tests.conftest import ACCEPTED_AT, CONSENT_FILE, VERSION


def write(tmp_path, payload):
    path = tmp_path / "telemetry-consent.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def without(name):
    document = copy.deepcopy(CONSENT_FILE)
    del document[name]
    return document


# ── the ordinary states ──────────────────────────────────────────────


def test_a_missing_file_means_nothing_was_accepted(tmp_path):
    """The state of every node in the fleet today — nothing writes it yet."""
    record = read_consent(tmp_path / "absent")

    assert record == NONE_GIVEN
    assert not record.complete
    assert not record.may_stream


def test_all_three_records_are_read(tmp_path):
    record = read_consent(write(tmp_path, CONSENT_FILE))

    assert record.licence == AcceptanceRecord(VERSION, ACCEPTED_AT)
    assert record.remote_management == AcceptanceRecord(VERSION, ACCEPTED_AT)
    assert record.publication == PublicationChoice(VERSION, ACCEPTED_AT, "public")
    assert record.complete


def test_only_the_licence_gates_streaming(tmp_path):
    """The spec is specific about this: withdrawing the publication choice must
    not stop the node."""
    record = read_consent(write(tmp_path, without("publication")))

    assert record.may_stream
    assert not record.complete  # but it still cannot register


def test_a_missing_licence_stops_streaming(tmp_path):
    record = read_consent(write(tmp_path, without("licence")))

    assert not record.may_stream


def test_which_records_are_missing_is_reported(tmp_path):
    """For the status document, which is the only route to an operator."""
    record = read_consent(write(tmp_path, without("remote_management")))

    assert record.missing == ["remote_management"]


def test_every_record_missing_is_listed(tmp_path):
    assert read_consent(tmp_path / "absent").missing == [
        "licence",
        "remote_management",
        "publication",
    ]


# ── the publication choice ───────────────────────────────────────────


def test_a_private_choice_is_carried(tmp_path):
    document = copy.deepcopy(CONSENT_FILE)
    document["publication"]["choice"] = "private"

    record = read_consent(write(tmp_path, document))

    assert record.publication.choice == "private"
    assert not record.publication.is_public


def test_a_missing_choice_is_never_defaulted(tmp_path):
    """The server treats an absent record as public, which is its decision about
    its own archive. Inventing one here would claim the owner made a choice they
    were never shown."""
    document = copy.deepcopy(CONSENT_FILE)
    del document["publication"]["choice"]

    record = read_consent(write(tmp_path, document))

    assert record.publication is None
    assert not record.complete


def test_an_unrecognised_choice_is_rejected(tmp_path):
    document = copy.deepcopy(CONSENT_FILE)
    document["publication"]["choice"] = "maybe"

    assert read_consent(write(tmp_path, document)).publication is None


def test_the_disclosure_version_is_kept(tmp_path):
    """It records which wording the owner actually saw, which is the point of
    versioning it separately."""
    document = copy.deepcopy(CONSENT_FILE)
    document["publication"]["version"] = "2027-01-15"

    assert read_consent(write(tmp_path, document)).publication.version == "2027-01-15"


# ── malformed input fails closed, per record ─────────────────────────


def test_malformed_json_yields_nothing(tmp_path):
    assert read_consent(write(tmp_path, "{not json")) == NONE_GIVEN


def test_a_non_object_yields_nothing(tmp_path):
    assert read_consent(write(tmp_path, [1, 2, 3])) == NONE_GIVEN


def test_a_record_missing_its_version_is_dropped(tmp_path):
    document = copy.deepcopy(CONSENT_FILE)
    del document["licence"]["version"]

    record = read_consent(write(tmp_path, document))

    assert record.licence is None
    assert record.remote_management is not None  # the others survive


def test_a_record_missing_accepted_at_is_dropped(tmp_path):
    document = copy.deepcopy(CONSENT_FILE)
    del document["remote_management"]["accepted_at"]

    assert read_consent(write(tmp_path, document)).remote_management is None


def test_a_non_object_record_is_dropped(tmp_path):
    document = copy.deepcopy(CONSENT_FILE)
    document["licence"] = "yes"

    assert read_consent(write(tmp_path, document)).licence is None


def test_accepted_at_is_passed_through_unvalidated(tmp_path):
    """Validating RFC 3339 is stage 2's job, where the generated model does it."""
    document = copy.deepcopy(CONSENT_FILE)
    document["licence"]["accepted_at"] = "soon"

    assert read_consent(write(tmp_path, document)).licence.accepted_at == "soon"


def test_never_raises(tmp_path):
    path = tmp_path / "telemetry-consent.json"
    path.mkdir()

    assert read_consent(path) == NONE_GIVEN
