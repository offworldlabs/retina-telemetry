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
        # regulatory callsign. See open question Q5.
        tx_name=_require(document, "location.tx.name", str),
        fc_hz=_require(document, "capture.fc", float),
        fs_hz=_require(document, "capture.fs", float),
        # Bins, not kilometres. Stage 2 derives max_range_km as
        # delay_max_bins * c / fs / 1000. See Q6.
        delay_max_bins=_require(document, "process.ambiguity.delayMax", int),
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

    if kind is str:
        if not isinstance(value, str):
            raise ConfigUnavailable(f"{dotted} must be a string, got {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigUnavailable(f"{dotted} must be numeric, got {value!r}")
    return kind(value)
