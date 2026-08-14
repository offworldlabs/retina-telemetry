"""The node's merged configuration.

``/data/retina-node/config/config.yml`` is produced by ``config-merger`` from
the packaged defaults plus ``user.yml`` at stack start, and is the source of
truth on the node. We mount it read-only.

**No conversion happens here.** Altitudes stay in metres and ``delayMax`` stays
in bins, under names that say so; ``wire/`` converts to the spec's feet and
derives ``max_range_km``. Deriving rather than storing that last one means it
can never disagree with what blah2 actually computes.

Every field here is sent. Change detection is dataclass equality — mapping the
document to a frozen dataclass already discards everything we do not send, so a
comment, a reformat, or an edit to an unmapped key produces an equal object and
cannot trigger a ``PUT /nodes/config``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("/data/retina-node/config/config.yml")

#: Where the beam geometry will live once retina-gui can write it. Nothing sets
#: these today — they are named here so the mapping is one edit when it does.
#: Under ``location.rx`` because the antenna is a receiver property and there is
#: no antenna section; if retina-gui puts them elsewhere, change these two
#: constants and nothing else.
BEAM_WIDTH_KEY = "location.rx.beam_width"
BEAM_AZIMUTH_KEY = "location.rx.beam_azimuth"

#: blah2-api's own fallbacks when the association tolerances are unset, from
#: `api/server.js:380-381`. Reading them here rather than in stage 2 because
#: they are a fact about the node stack, not about the wire.
DELAY_TOLERANCE_KEY = "truth.adsb.delay_tolerance"
DOPPLER_TOLERANCE_KEY = "truth.adsb.doppler_tolerance"

BLAH2_API_DELAY_TOLERANCE_KM = 2.0
BLAH2_API_DOPPLER_TOLERANCE_HZ = 5.0


class ConfigUnavailable(Exception):
    """The configuration cannot be read or is missing something required.

    Raised rather than defaulted: the caller decides whether to keep
    heartbeating without it. Every local source is optional at the process
    level, but a *wrong* configuration is worse than an absent one.
    """


@dataclass(frozen=True)
class NodeConfigRaw:
    """Configuration in the units the node stores it in.

    Field names carry the source unit wherever it differs from the spec's, so
    that a missing conversion in stage 2 is visible at the call site.

    Frozen, so ``==`` is the change check.
    """

    rx_lat: float
    rx_lon: float
    rx_alt_m: float
    tx_lat: float
    tx_lon: float
    tx_alt_m: float
    tx_name: str
    fc_hz: float
    fs_hz: float
    delay_max_bins: int

    #: Coherent processing interval, seconds, straight from ``process.data.cpi``.
    #: The spec's ``cpi_s`` in the same unit, so no conversion — it is here
    #: because the server needs the width of the window every ``t`` closes.
    #:
    #: Note this is the *configured* CPI, not the observed frame period. They
    #: differ by roughly 2x on current hardware because processing overruns; see
    #: ``docs/data-sources.md`` §2. Nothing should derive a cadence from it.
    cpi_s: float

    #: ADS-B association gate, in blah2's own units. `delay_tolerance` is
    #: compared against a bistatic range in **kilometres** — `computeBistaticDelay`
    #: returns `bistatic_range / 1000` from metric ECEF (`api/bistatic.js:67`) and
    #: is compared to `det.delay` at `api/server.js:382`. Stage 2 converts to the
    #: microseconds the spec asks for, which is why the spec's 6.67 example is our
    #: 2.0 km.
    #:
    #: Defaulted rather than required because `api/server.js:380-381` falls back to
    #: exactly these values when the keys are absent. Substituting them is
    #: therefore reporting the tolerance blah2-api actually applies, not guessing
    #: one — the opposite of the beam fields below.
    delay_tolerance_km: float = BLAH2_API_DELAY_TOLERANCE_KM
    doppler_tolerance_hz: float = BLAH2_API_DOPPLER_TOLERANCE_HZ

    # ── antenna geometry: optional, and absent on every node ─────
    # Required by the spec but **nullable** since v1.1.1, so an uncharacterised
    # antenna sends explicit nulls rather than being unable to build a config at
    # all. retina-gui does not collect the geometry from owners and is not
    # scheduled to, so absent is the fleet-wide steady state.
    #
    # Nothing is substituted. Unlike the tolerances above there is no value the
    # node stack actually applies — a beam width would be invented, and the
    # server could not tell.
    beam_width_deg: float | None = None
    beam_azimuth_deg: float | None = None


def read_config(path: Path | str = DEFAULT_CONFIG_PATH) -> NodeConfigRaw:
    """Read and validate the merged node configuration.

    Raises:
        ConfigUnavailable: if the file is missing, unparseable, or lacks a
            field the server requires.
    """
    path = Path(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigUnavailable(f"{path} does not exist") from exc
    except OSError as exc:
        raise ConfigUnavailable(f"{path} could not be read: {exc}") from exc

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigUnavailable(f"{path} is not valid YAML: {exc}") from exc

    if not isinstance(document, dict):
        raise ConfigUnavailable(f"{path} does not contain a mapping")

    return NodeConfigRaw(
        rx_lat=_require(document, "location.rx.latitude", float),
        rx_lon=_require(document, "location.rx.longitude", float),
        rx_alt_m=_require(document, "location.rx.altitude", float),
        tx_lat=_require(document, "location.tx.latitude", float),
        tx_lon=_require(document, "location.tx.longitude", float),
        tx_alt_m=_require(document, "location.tx.altitude", float),
        # A free-text display name the operator typed in the tower step, not a
        # regulatory callsign.
        tx_name=_require(document, "location.tx.name", str),
        fc_hz=_require(document, "capture.fc", float),
        fs_hz=_require(document, "capture.fs", float),
        # Bins, not kilometres. Stage 2 derives max_range_km as
        # delay_max_bins * c / fs / 1000.
        delay_max_bins=_require(document, "process.ambiguity.delayMax", int),
        # Seconds. Required by the spec since v1.1.1 as cpi_s: it is the width
        # of the window every DetectionFrame.t closes, so the server cannot know
        # what a frame's samples span without it. blah2 cannot run without this
        # key, so a missing one is a malformed configuration rather than a gap.
        cpi_s=_require(document, "process.data.cpi", float),
        delay_tolerance_km=_optional(document, DELAY_TOLERANCE_KEY, float)
        or BLAH2_API_DELAY_TOLERANCE_KM,
        doppler_tolerance_hz=_optional(document, DOPPLER_TOLERANCE_KEY, float)
        or BLAH2_API_DOPPLER_TOLERANCE_HZ,
        # Optional here and nullable on the wire, so an unset key travels as an
        # explicit null rather than blocking anything.
        beam_width_deg=_optional(document, BEAM_WIDTH_KEY, float),
        beam_azimuth_deg=_optional(document, BEAM_AZIMUTH_KEY, float),
    )


def _walk(document: dict[str, Any], dotted: str) -> Any:
    node: Any = document
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _optional(document: dict[str, Any], dotted: str, kind: type) -> Any:
    value = _walk(document, dotted)
    if value is None:
        return None
    return _coerce(value, dotted, kind)


def _require(document: dict[str, Any], dotted: str, kind: type) -> Any:
    value = _walk(document, dotted)
    if value is None:
        raise ConfigUnavailable(f"required key {dotted} is missing")
    return _coerce(value, dotted, kind)


def _coerce(value: Any, dotted: str, kind: type) -> Any:
    if kind is str:
        if not isinstance(value, str):
            raise ConfigUnavailable(f"{dotted} must be a string, got {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigUnavailable(f"{dotted} must be numeric, got {value!r}")
    return kind(value)
