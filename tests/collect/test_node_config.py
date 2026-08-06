import copy

import pytest
import yaml

from retina_telemetry.collect.node_config import ConfigUnavailable, read_config

# Mirrors retina-node/config/default.yml, trimmed to the keys we map.
DEFAULTS = {
    "capture": {"fs": 2000000, "fc": 503000000},
    "process": {
        "data": {"cpi": 0.5, "buffer": 1.5},
        "ambiguity": {"delayMin": -10, "delayMax": 400},
    },
    "truth": {
        "adsb": {
            "enabled": True,
            "delay_tolerance": 2.0,
            "doppler_tolerance": 5.0,
        }
    },
    "location": {
        "rx": {"latitude": 51.4769, "longitude": -0.0005, "altitude": 48, "name": "Greenwich"},
        "tx": {
            "latitude": 51.4244,
            "longitude": -0.0753,
            "altitude": 219,
            "name": "Crystal Palace",
        },
    },
}


def write(tmp_path, document=None, *, text=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.yml"
    path.write_text(text if text is not None else yaml.safe_dump(document), encoding="utf-8")
    return path


def test_maps_every_field(tmp_path):
    config = read_config(write(tmp_path, DEFAULTS)).config

    assert config.rx_lat == 51.4769
    assert config.rx_lon == -0.0005
    assert config.tx_lat == 51.4244
    assert config.tx_name == "Crystal Palace"
    assert config.fc_hz == 503000000
    assert config.fs_hz == 2000000
    assert config.adsb_enabled is True
    assert config.adsb_delay_tolerance == 2.0
    assert config.adsb_doppler_tolerance == 5.0


def test_altitudes_stay_in_metres(tmp_path):
    """The spec wants feet; converting here would put this out of step with
    data-sources.md and hide the conversion from stage 2's call site."""
    config = read_config(write(tmp_path, DEFAULTS)).config

    assert config.rx_alt_m == 48.0
    assert config.tx_alt_m == 219.0


def test_delay_max_stays_in_bins(tmp_path):
    """max_range_km is derived in stage 2 so it can never disagree with what
    blah2 actually computes."""
    config = read_config(write(tmp_path, DEFAULTS)).config

    assert config.delay_max_bins == 400


def test_cpi_is_carried_even_though_the_spec_has_no_slot(tmp_path):
    """Needed for the staleness window and for capture-gap detection."""
    assert read_config(write(tmp_path, DEFAULTS)).config.cpi_s == 0.5


def test_absent_adsb_tolerances_are_none(tmp_path):
    document = copy.deepcopy(DEFAULTS)
    document["truth"]["adsb"] = {"enabled": False}

    config = read_config(write(tmp_path, document)).config

    assert config.adsb_enabled is False
    assert config.adsb_delay_tolerance is None


def test_absent_adsb_section_defaults_to_disabled(tmp_path):
    document = copy.deepcopy(DEFAULTS)
    del document["truth"]

    assert read_config(write(tmp_path, document)).config.adsb_enabled is False


# ── change detection ─────────────────────────────────────────────────


def test_digest_is_stable_across_reads(tmp_path):
    path = write(tmp_path, DEFAULTS)

    assert read_config(path).digest == read_config(path).digest


def test_digest_ignores_formatting_and_comments(tmp_path):
    """Hashing the mapped values rather than the file means a reformat does
    not trigger a PUT /nodes/config."""
    plain = read_config(write(tmp_path / "a", DEFAULTS))

    reformatted = yaml.safe_dump(DEFAULTS, default_flow_style=True, width=40)
    commented = read_config(write(tmp_path / "b", text="# a comment\n" + reformatted))

    assert plain.digest == commented.digest


def test_digest_ignores_keys_we_do_not_send(tmp_path):
    document = copy.deepcopy(DEFAULTS)
    document["save"] = {"iq": True}
    document["process"]["detection"] = {"pfa": 0.001}

    assert read_config(write(tmp_path / "a", DEFAULTS)).digest == (
        read_config(write(tmp_path / "b", document)).digest
    )


def test_digest_changes_when_a_mapped_value_changes(tmp_path):
    moved = copy.deepcopy(DEFAULTS)
    moved["location"]["rx"]["latitude"] = 51.5

    assert read_config(write(tmp_path / "a", DEFAULTS)).digest != (
        read_config(write(tmp_path / "b", moved)).digest
    )


def test_changed_from_detects_a_first_read(tmp_path):
    snapshot = read_config(write(tmp_path, DEFAULTS))

    assert snapshot.changed_from(None)
    assert not snapshot.changed_from(snapshot)


# ── failure modes ────────────────────────────────────────────────────


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigUnavailable, match="does not exist"):
        read_config(tmp_path / "absent")


def test_invalid_yaml_raises(tmp_path):
    with pytest.raises(ConfigUnavailable, match="not valid YAML"):
        read_config(write(tmp_path, text="key: [unclosed\n"))


def test_non_mapping_raises(tmp_path):
    with pytest.raises(ConfigUnavailable, match="does not contain a mapping"):
        read_config(write(tmp_path, text="- one\n- two\n"))


@pytest.mark.parametrize(
    "dotted",
    [
        "location.rx.latitude",
        "location.tx.altitude",
        "location.tx.name",
        "capture.fc",
        "capture.fs",
        "process.data.cpi",
        "process.ambiguity.delayMax",
    ],
)
def test_missing_required_key_raises(tmp_path, dotted):
    document = copy.deepcopy(DEFAULTS)
    node = document
    *parents, leaf = dotted.split(".")
    for part in parents:
        node = node[part]
    del node[leaf]

    with pytest.raises(ConfigUnavailable, match=dotted):
        read_config(write(tmp_path, document))


def test_non_numeric_coordinate_raises(tmp_path):
    document = copy.deepcopy(DEFAULTS)
    document["location"]["rx"]["latitude"] = "north"

    with pytest.raises(ConfigUnavailable, match="must be numeric"):
        read_config(write(tmp_path, document))


def test_non_string_tx_name_raises(tmp_path):
    document = copy.deepcopy(DEFAULTS)
    document["location"]["tx"]["name"] = 12345

    with pytest.raises(ConfigUnavailable, match="must be a string"):
        read_config(write(tmp_path, document))
