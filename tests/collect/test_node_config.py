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
    config = read_config(write(tmp_path, DEFAULTS))

    assert config.rx_lat == 51.4769
    assert config.rx_lon == -0.0005
    assert config.tx_lat == 51.4244
    assert config.tx_name == "Crystal Palace"
    assert config.fc_hz == 503000000
    assert config.fs_hz == 2000000


def test_altitudes_stay_in_metres(tmp_path):
    """The spec wants feet; converting here would put this out of step with
    data-sources.md and hide the conversion from stage 2's call site."""
    config = read_config(write(tmp_path, DEFAULTS))

    assert config.rx_alt_m == 48.0
    assert config.tx_alt_m == 219.0


def test_delay_max_stays_in_bins(tmp_path):
    """max_range_km is derived in stage 2 so it can never disagree with what
    blah2 actually computes."""
    assert read_config(write(tmp_path, DEFAULTS)).delay_max_bins == 400


def test_collects_only_what_is_sent(tmp_path):
    """Ten fields, every one of them feeding NodeConfig. cpi, the ADS-B flag and
    the association tolerances were all collected at some point and are not any
    more — nothing local needs them and the spec has no field for them."""
    config = read_config(write(tmp_path, DEFAULTS))

    assert set(vars(config)) == {
        "rx_lat",
        "rx_lon",
        "rx_alt_m",
        "tx_lat",
        "tx_lon",
        "tx_alt_m",
        "tx_name",
        "fc_hz",
        "fs_hz",
        "delay_max_bins",
    }


# ── change detection is dataclass equality ───────────────────────────


def test_identical_documents_compare_equal(tmp_path):
    assert read_config(write(tmp_path / "a", DEFAULTS)) == read_config(
        write(tmp_path / "b", DEFAULTS)
    )


def test_formatting_and_comments_do_not_count_as_a_change(tmp_path):
    """Mapping to a frozen dataclass discards everything we do not send, so a
    reformat cannot trigger a PUT /nodes/config."""
    plain = read_config(write(tmp_path / "a", DEFAULTS))

    reformatted = yaml.safe_dump(DEFAULTS, default_flow_style=True, width=40)
    commented = read_config(write(tmp_path / "b", text="# a comment\n" + reformatted))

    assert plain == commented


def test_unmapped_keys_do_not_count_as_a_change(tmp_path):
    document = copy.deepcopy(DEFAULTS)
    document["save"] = {"iq": True}
    document["process"]["detection"] = {"pfa": 0.001}
    document["truth"]["adsb"]["delay_tolerance"] = 9.9

    assert read_config(write(tmp_path / "a", DEFAULTS)) == read_config(
        write(tmp_path / "b", document)
    )


def test_a_moved_receiver_counts_as_a_change(tmp_path):
    moved = copy.deepcopy(DEFAULTS)
    moved["location"]["rx"]["latitude"] = 51.5

    assert read_config(write(tmp_path / "a", DEFAULTS)) != read_config(write(tmp_path / "b", moved))


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
