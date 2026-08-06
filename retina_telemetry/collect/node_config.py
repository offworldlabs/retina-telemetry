"""The node's merged configuration.

``/data/retina-node/config/config.yml`` is produced by ``config-merger`` from
the packaged defaults plus ``user.yml`` at stack start, and is the source of
truth on the node. We mount it read-only.

**No conversion happens here.** Altitudes stay in metres and ``delayMax`` stays
in bins, under names that say so; ``wire/`` converts to the spec's feet and
derives ``max_range_km``. Deriving rather than storing that last one means it
can never disagree with what blah2 actually computes.

Change detection hashes the *mapped values*, not the file. A comment, a
reordering, or an edit to a key we do not send should not cause a
``PUT /nodes/config``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("/data/retina-node/config/config.yml")


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
    cpi_s: float
    delay_max_bins: int
    adsb_enabled: bool


@dataclass(frozen=True)
class ConfigSnapshot:
    config: NodeConfigRaw
    digest: str

    def changed_from(self, other: ConfigSnapshot | None) -> bool:
        return other is None or self.digest != other.digest


def read_config(path: Path | str = DEFAULT_CONFIG_PATH) -> ConfigSnapshot:
    """Read, validate and hash the merged node configuration.

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

    config = _map(document)
    return ConfigSnapshot(config=config, digest=_digest(config))


def _map(document: dict[str, Any]) -> NodeConfigRaw:
    return NodeConfigRaw(
        rx_lat=_require(document, "location.rx.latitude", float),
        rx_lon=_require(document, "location.rx.longitude", float),
        rx_alt_m=_require(document, "location.rx.altitude", float),
        tx_lat=_require(document, "location.tx.latitude", float),
        tx_lon=_require(document, "location.tx.longitude", float),
        tx_alt_m=_require(document, "location.tx.altitude", float),
        # A free-text display name the operator typed in the tower step, not a
        # regulatory callsign. See open question Q5.
        tx_name=_require(document, "location.tx.name", str),
        fc_hz=_require(document, "capture.fc", float),
        fs_hz=_require(document, "capture.fs", float),
        # Not sent — the spec has no field for it (Q3 proposes one). Collected
        # because it seeds the staleness window that derives NodeHealth.blah2.
        cpi_s=_require(document, "process.data.cpi", float),
        # Bins, not kilometres. Stage 2 derives max_range_km as
        # delay_max_bins * c / fs / 1000. See Q6.
        delay_max_bins=_require(document, "process.ambiguity.delayMax", int),
        # Not sent. Needed locally to tell "ADS-B is off" from "ADS-B is broken"
        # when a polled frame carries no adsb key, which is what NodeHealth.adsb
        # reports. The association tolerances beside it in config are not
        # collected: Q7 proposes sending them, but the spec has no field today.
        adsb_enabled=_optional(document, "truth.adsb.enabled", bool) or False,
    )


def _walk(document: dict[str, Any], dotted: str) -> Any:
    node: Any = document
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _require(document: dict[str, Any], dotted: str, kind: type) -> Any:
    value = _walk(document, dotted)
    if value is None:
        raise ConfigUnavailable(f"required key {dotted} is missing")
    return _coerce(value, dotted, kind)


def _optional(document: dict[str, Any], dotted: str, kind: type) -> Any:
    value = _walk(document, dotted)
    if value is None:
        return None
    return _coerce(value, dotted, kind)


def _coerce(value: Any, dotted: str, kind: type) -> Any:
    if kind is bool:
        if not isinstance(value, bool):
            raise ConfigUnavailable(f"{dotted} must be a boolean, got {value!r}")
        return value
    if kind is str:
        if not isinstance(value, str):
            raise ConfigUnavailable(f"{dotted} must be a string, got {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigUnavailable(f"{dotted} must be numeric, got {value!r}")
    return kind(value)


def _digest(config: NodeConfigRaw) -> str:
    canonical = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
