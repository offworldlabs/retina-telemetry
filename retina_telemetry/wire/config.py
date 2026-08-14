"""``NodeConfigRaw`` → ``NodeConfig``.

Sent in two places — inside ``RegisterRequest`` and on its own via
``PUT /nodes/config`` — so it is built once here and used by both.
"""

from __future__ import annotations

from retina_telemetry.collect.node_config import NodeConfigRaw
from retina_telemetry.wire.models import NodeConfig
from retina_telemetry.wire.units import m_to_ft, max_range_km, tolerance_km_to_us


def build_node_config(config: NodeConfigRaw) -> NodeConfig:
    """Convert the node's stored configuration into the wire payload.

    | Wire field | Source | Conversion |
    |---|---|---|
    | ``rx_lat`` / ``rx_lon`` | ``config.rx_lat`` / ``rx_lon`` | none, degrees both sides |
    | ``rx_alt_ft`` | ``config.rx_alt_m`` | **× 3.28084** |
    | ``tx_lat`` / ``tx_lon`` | ``config.tx_lat`` / ``tx_lon`` | none |
    | ``tx_alt_ft`` | ``config.tx_alt_m`` | **× 3.28084** |
    | ``tx_callsign`` | ``config.tx_name`` | none |
    | ``fc_hz`` / ``fs_hz`` | ``config.fc_hz`` / ``fs_hz`` | none |
    | ``max_range_km`` | ``config.delay_max_bins``, ``config.fs_hz`` | × c ÷ fs ÷ 1000 |
    | ``beam_width_deg`` | ``config.beam_width_deg`` | none — ``null`` if unset |
    | ``beam_azimuth_deg`` | ``config.beam_azimuth_deg`` | none — ``null`` if unset |
    | ``cpi_s`` | ``config.cpi_s`` | none, seconds both sides |
    | ``delay_tolerance_us`` | ``config.delay_tolerance_km`` | **× 3.335641** |
    | ``doppler_tolerance_hz`` | ``config.doppler_tolerance_hz`` | none, Hz both sides |

    **Both beam fields are optional and nothing is substituted for them.** The
    spec made them optional on 2026-08-11, so an uncharacterised antenna simply
    omits both keys — no value the node did not give us reaches the server,
    which is the same discipline as the consent records.

    That is not a temporary state. retina-gui is not collecting the geometry
    from owners for the foreseeable future, so **absent is the normal case on
    every node in the fleet**, not an edge to be tidied up later. Treat a
    populated beam width as the exception when reading this.

    ``tx_callsign`` carries ``location.tx.name``, which is free text the
    operator typed in the tower step (e.g. "Crystal Palace") rather than a
    regulatory callsign.

    Raises:
        ValueError: if ``fs_hz`` is not positive, via :func:`max_range_km`.
    """
    return NodeConfig(
        rx_lat=config.rx_lat,
        rx_lon=config.rx_lon,
        rx_alt_ft=m_to_ft(config.rx_alt_m),
        tx_lat=config.tx_lat,
        tx_lon=config.tx_lon,
        tx_alt_ft=m_to_ft(config.tx_alt_m),
        tx_callsign=config.tx_name,
        fc_hz=config.fc_hz,
        fs_hz=config.fs_hz,
        beam_width_deg=config.beam_width_deg,
        beam_azimuth_deg=config.beam_azimuth_deg,
        max_range_km=max_range_km(config.delay_max_bins, config.fs_hz),
        cpi_s=config.cpi_s,
        delay_tolerance_us=tolerance_km_to_us(config.delay_tolerance_km),
        doppler_tolerance_hz=config.doppler_tolerance_hz,
    )
