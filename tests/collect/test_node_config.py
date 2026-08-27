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


# ── beam geometry scaffolding ───────────────────────────────────


def test_beam_fields_are_absent_on_a_node_today(tmp_path):
    """Nothing writes them yet, so reading must not raise — raising would make
    every node in the fleet unreadable."""
    config = read_config(write(tmp_path, DEFAULTS))

    assert config.beam_width_deg is None
    assert config.beam_azimuth_deg is None


def test_beam_fields_are_read_when_present(tmp_path):
    """The seam retina-gui's work lands in: config change, not code change."""
    document = copy.deepcopy(DEFAULTS)
    document["location"]["rx"]["beam_width"] = 60
    document["location"]["rx"]["beam_azimuth"] = 135.5

    config = read_config(write(tmp_path, document))

    assert config.beam_width_deg == 60.0
    assert config.beam_azimuth_deg == 135.5


def test_omnidirectional_azimuth_is_indistinguishable_from_unset(tmp_path):
    """Both are None, and that is fine — the spec asks for null rather than 0.0
    for broadside, and every node in the fleet leaves it unset."""
    document = copy.deepcopy(DEFAULTS)
    document["location"]["rx"]["beam_width"] = 60

    config = read_config(write(tmp_path, document))

    assert config.beam_width_deg == 60.0
    assert config.beam_azimuth_deg is None


def test_a_configured_beam_counts_as_a_change(tmp_path):
    directional = copy.deepcopy(DEFAULTS)
    directional["location"]["rx"]["beam_width"] = 60

    assert read_config(write(tmp_path / "a", DEFAULTS)) != read_config(
        write(tmp_path / "b", directional)
    )


def test_non_numeric_beam_width_raises(tmp_path):
    document = copy.deepcopy(DEFAULTS)
    document["location"]["rx"]["beam_width"] = "wide"

    with pytest.raises(ConfigUnavailable, match="must be numeric"):
        read_config(write(tmp_path, document))


def test_collects_only_what_is_sent(tmp_path):
    """Every field here feeds NodeConfig, and nothing else is read.

    Three of them came back in spec v1.1.1 after being deliberately removed:
    cpi, and the two ADS-B association tolerances. That is the discipline
    working rather than failing — they went because the spec had no field for
    them, and returned through the spec rather than by us deciding they looked
    useful. `truth.adsb.enabled` stayed gone, because the presence of the
    `adsb` key on a polled frame *is* the flag.

    `delay_max_bins` is still the one thing collected that is never sent: it
    exists only because `max_range_km` is derived from it.
    """
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
        "cpi_s",
        "delay_tolerance_km",
        "doppler_tolerance_hz",
        "beam_width_deg",
        "beam_azimuth_deg",
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

    assert read_config(write(tmp_path / "a", DEFAULTS)) == read_config(
        write(tmp_path / "b", document)
    )


def test_an_edited_association_tolerance_counts_as_a_change(tmp_path):
    """It used to be unmapped, so editing it changed nothing. v1.1.1 requires
    it on the wire, so it now belongs to the config the server holds and an
    edit has to trigger a resend."""
    document = copy.deepcopy(DEFAULTS)
    document["truth"]["adsb"]["delay_tolerance"] = 9.9

    assert read_config(write(tmp_path / "a", DEFAULTS)) != read_config(
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


# ── geometry: nullable, and unset until an owner picks a tower ───────


def test_a_null_geometry_reads_as_unlocated(tmp_path):
    """What retina-node's default.yml will ship. Must not raise: an unsited
    node is ordinary, not a malformed config."""
    document = copy.deepcopy(DEFAULTS)
    for end in ("rx", "tx"):
        document["location"][end] = {
            "latitude": None,
            "longitude": None,
            "altitude": None,
            "name": None,
        }

    config = read_config(write(tmp_path, document))

    assert config.is_located is False
    assert config.rx_lat is None


def test_a_full_geometry_is_located(tmp_path):
    assert read_config(write(tmp_path, DEFAULTS)).is_located is True


def test_a_partial_geometry_is_not_located(tmp_path):
    """The bistatic solution needs all of it, and a missing value becomes NaN
    downstream rather than an error."""
    document = copy.deepcopy(DEFAULTS)
    document["location"]["tx"]["longitude"] = None

    assert read_config(write(tmp_path, document)).is_located is False


def test_zero_is_a_real_coordinate(tmp_path):
    """Testing for truthiness would report a node on the equator as unsited."""
    document = copy.deepcopy(DEFAULTS)
    for end in ("rx", "tx"):
        document["location"][end].update(latitude=0, longitude=0, altitude=0)

    assert read_config(write(tmp_path, document)).is_located is True


def test_a_missing_transmitter_name_does_not_unlocate_a_node(tmp_path):
    """A name is a label, not a position."""
    document = copy.deepcopy(DEFAULTS)
    document["location"]["tx"]["name"] = None

    config = read_config(write(tmp_path, document))

    assert config.is_located is True
    assert config.tx_name is None


def test_a_wrongly_typed_coordinate_still_raises(tmp_path):
    """Optional means "may be absent", not "may be anything"."""
    document = copy.deepcopy(DEFAULTS)
    document["location"]["rx"]["latitude"] = "fifty-one"

    with pytest.raises(ConfigUnavailable, match="location.rx.latitude"):
        read_config(write(tmp_path, document))
