import pytest

from retina_telemetry.collect.identity import IdentityUnavailable, read_node_id


def write(tmp_path, contents):
    path = tmp_path / "node_id"
    path.write_text(contents, encoding="utf-8")
    return path


def test_reads_a_valid_node_id(tmp_path):
    assert read_node_id(write(tmp_path, "ret1a2b3c4d")) == "ret1a2b3c4d"


def test_trailing_newline_is_stripped(tmp_path):
    """The identity script echoes the value, so a trailing newline is normal."""
    assert read_node_id(write(tmp_path, "ret1a2b3c4d\n")) == "ret1a2b3c4d"


def test_missing_file_raises(tmp_path):
    with pytest.raises(IdentityUnavailable, match="does not exist"):
        read_node_id(tmp_path / "absent")


def test_empty_file_raises(tmp_path):
    with pytest.raises(IdentityUnavailable, match="is empty"):
        read_node_id(write(tmp_path, "\n"))


def test_directory_instead_of_file_raises(tmp_path):
    with pytest.raises(IdentityUnavailable):
        read_node_id(tmp_path)


@pytest.mark.parametrize(
    ("contents", "why"),
    [
        ("Unknown", "retina-gui's get_node_id() returns this string on failure"),
        ("ret000000000", "the placeholder in retina-node/config/default.yml"),
        ("ret1A2B3C4D", "uppercase hex does not match the spec pattern"),
        ("ret1a2b3c", "too short"),
        ("ret1a2b3c4d5", "too long"),
        ("mac=b8:27:eb:00:11:22", "the identity script's non-Pi fallback"),
        ("ret1a2b3c4z", "z is not hex"),
        ("RET1a2b3c4d", "prefix must be lowercase"),
    ],
)
def test_invalid_identities_raise(tmp_path, contents, why):
    """A node that invents its own identity causes more trouble than one that
    will not start, so every one of these is a hard error rather than a
    fallback."""
    with pytest.raises(IdentityUnavailable):
        read_node_id(write(tmp_path, contents))


def test_never_returns_a_default(tmp_path):
    """The whole point of this module: there is no code path that produces a
    value the file did not contain."""
    for contents in ["", "Unknown", "ret000000000", "garbage"]:
        try:
            result = read_node_id(write(tmp_path, contents))
        except IdentityUnavailable:
            continue
        assert result == contents.strip(), "a value appeared that was not in the file"
