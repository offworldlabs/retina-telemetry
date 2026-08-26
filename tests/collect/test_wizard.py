"""Tests for the setup-wizard-completed flag.

The path is a cross-repo contract with retina-gui's `device_state.py`, so these
assert the default location by name rather than only round-tripping a tmp file.
"""

from pathlib import Path

from retina_telemetry.collect.wizard import DEFAULT_WIZARD_FLAG_PATH, setup_complete


def test_no_flag_is_the_ordinary_state_before_setup(tmp_path):
    """Every node starts here. Not an error, and not reported as one."""
    assert setup_complete(tmp_path / "setup-wizard-completed") is False


def test_a_present_flag_is_complete(tmp_path):
    flag = tmp_path / "setup-wizard-completed"
    flag.write_text("2026-06-30T08:18:00")

    assert setup_complete(flag) is True


def test_the_contents_are_not_parsed(tmp_path):
    """retina-gui owns that format. Needing to read it would make a second
    thing to keep in step across two repos, so existence is the whole signal."""
    flag = tmp_path / "setup-wizard-completed"
    flag.write_text("not a timestamp at all")

    assert setup_complete(flag) is True


def test_an_empty_flag_still_counts(tmp_path):
    """An interrupted write leaves a node configured but silent otherwise, and
    retina-gui writes this after the wizard has already finished."""
    flag = tmp_path / "setup-wizard-completed"
    flag.touch()

    assert setup_complete(flag) is True


def test_a_directory_in_its_place_is_not_a_flag(tmp_path):
    """`Path.exists()` is true of a directory, which would open the gate on
    something retina-gui never wrote."""
    flag = tmp_path / "setup-wizard-completed"
    flag.mkdir()

    assert setup_complete(flag) is False


def test_accepts_a_string_path(tmp_path):
    """Settings hands over a Path, but the sibling readers all take either."""
    flag = tmp_path / "setup-wizard-completed"
    flag.write_text("2026-06-30T08:18:00")

    assert setup_complete(str(flag)) is True


def test_the_default_path_is_the_contract_with_retina_gui():
    """retina-gui writes this exact path, and the container mounts
    /data/retina-gui read-only to read it. Changing either end alone strands
    every node in the fleet."""
    assert Path("/data/retina-gui/setup-wizard-completed") == DEFAULT_WIZARD_FLAG_PATH
