import json

from retina_telemetry.collect.consent import DENIED, Agreement, read_consent

AGREEMENT = {"version": "2026-07-01", "accepted_at": "2026-07-31T09:12:00Z"}


def write(tmp_path, payload):
    path = tmp_path / "telemetry-consent.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def test_missing_file_is_not_opted_in(tmp_path):
    """The ordinary state before anyone has been through the wizard — and the
    state on every node today, since nothing writes this file yet."""
    consent = read_consent(tmp_path / "absent")

    assert consent == DENIED
    assert not consent.may_transmit


def test_opted_in_with_agreement_may_transmit(tmp_path):
    consent = read_consent(write(tmp_path, {"opted_in": True, "agreement": AGREEMENT}))

    assert consent.opted_in
    assert consent.agreement == Agreement("2026-07-01", "2026-07-31T09:12:00Z")
    assert consent.may_transmit


def test_opted_in_without_agreement_may_not_transmit(tmp_path):
    """Registration requires the agreement object, so the opt-in alone is not
    enough."""
    consent = read_consent(write(tmp_path, {"opted_in": True}))

    assert consent.opted_in
    assert consent.agreement is None
    assert not consent.may_transmit


def test_opted_out_preserves_the_acceptance_record(tmp_path):
    """Accepting the terms and consenting to telemetry are separate facts; the
    owner may opt in later without re-accepting."""
    consent = read_consent(write(tmp_path, {"opted_in": False, "agreement": AGREEMENT}))

    assert not consent.opted_in
    assert consent.agreement == Agreement("2026-07-01", "2026-07-31T09:12:00Z")
    assert not consent.may_transmit


def test_malformed_json_fails_closed(tmp_path):
    assert read_consent(write(tmp_path, "{not json")) == DENIED


def test_non_object_fails_closed(tmp_path):
    assert read_consent(write(tmp_path, [1, 2, 3])) == DENIED


def test_non_boolean_opted_in_fails_closed(tmp_path):
    """ "true" is not true. If we cannot read what the owner agreed to, we do
    not transmit."""
    assert read_consent(write(tmp_path, {"opted_in": "true"})) == DENIED


def test_agreement_missing_version_is_dropped(tmp_path):
    consent = read_consent(
        write(tmp_path, {"opted_in": True, "agreement": {"accepted_at": "2026-07-31T09:12:00Z"}})
    )

    assert consent.agreement is None
    assert not consent.may_transmit


def test_agreement_missing_accepted_at_is_dropped(tmp_path):
    consent = read_consent(
        write(tmp_path, {"opted_in": True, "agreement": {"version": "2026-07-01"}})
    )

    assert consent.agreement is None


def test_non_object_agreement_is_dropped(tmp_path):
    consent = read_consent(write(tmp_path, {"opted_in": True, "agreement": "yes"}))

    assert consent.agreement is None


def test_accepted_at_is_passed_through_unvalidated(tmp_path):
    """Validating RFC 3339 is stage 2's job. This layer reports what the file
    says, so a bad timestamp surfaces at the boundary that cares."""
    consent = read_consent(
        write(tmp_path, {"opted_in": True, "agreement": {"version": "1", "accepted_at": "soon"}})
    )

    assert consent.agreement == Agreement("1", "soon")


def test_never_raises(tmp_path):
    path = tmp_path / "telemetry-consent.json"
    path.mkdir()

    assert read_consent(path) == DENIED
