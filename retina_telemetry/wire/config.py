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
    | ``rx_lat`` / ``rx_lon`` | ``config.rx_lat`` / ``rx_lon`` | none, degrees both sides; ``null`` if unsited |
    | ``rx_alt_ft`` | ``config.rx_alt_m`` | **× 3.28084**; ``null`` if unsited |
    | ``tx_lat`` / ``tx_lon`` | ``config.tx_lat`` / ``tx_lon`` | none; ``null`` if unsited |
    | ``tx_alt_ft`` | ``config.tx_alt_m`` | **× 3.28084**; ``null`` if unsited |
    | ``tx_callsign`` | ``config.tx_name`` | none; ``null`` if unsited |
    | ``fc_hz`` / ``fs_hz`` | ``config.fc_hz`` / ``fs_hz`` | none |
    | ``max_range_km`` | ``config.delay_max_bins``, ``config.fs_hz`` | × c ÷ fs ÷ 1000 |
    | ``beam_width_deg`` | ``config.beam_width_deg`` | none — ``null`` if unset |
    | ``beam_azimuth_deg`` | ``config.beam_azimuth_deg`` | none — ``null`` if unset |
    | ``cpi_s`` | ``config.cpi_s`` | none, seconds both sides |
    | ``delay_tolerance_us`` | ``config.delay_tolerance_km`` | **× 3.335641** |
    | ``doppler_tolerance_hz`` | ``config.doppler_tolerance_hz`` | none, Hz both sides |

    **Both beam fields are nullable and nothing is substituted for them.** They
    have been required *and* nullable since v1.1.1, so an uncharacterised antenna
    sends two explicit nulls: ``null`` says "not characterised" where an absent
    key would say nothing at all.

    The same holds for every other field here: no value the node did not give us
    reaches the server, which is the same discipline as the consent records.
    There are no substitutions in this module at all.

    That is not a temporary state. retina-gui is not collecting the geometry
    from owners for the foreseeable future, so **null is the normal case on
    every node in the fleet**, not an edge to be tidied up later. Treat a
    populated beam width as the exception when reading this.

    ``tx_callsign`` carries ``location.tx.name``, which is free text the
    operator typed in the tower step (e.g. "Crystal Palace") rather than a
    regulatory callsign.

    **An unsited node builds a configuration rather than failing to.** Spec
    v1.2.0 made the six coordinates nullable precisely so that a node whose
    owner has not picked a tower can register, stream and be counted; it simply
    places nothing on the map until a position arrives. So the geometry passes
    through as it is read, nulls included, and nothing here decides whether the
    node is sited: ``NodeConfigRaw.is_located`` answers that for the status
    document, and stage 3 no longer gates registration on it.

    ``tx_callsign`` travels the same way. v1.2.0 made only the coordinates
    nullable and left it at ``minLength: 1``, which meant an unsited node still
    could not build a payload: a tower's name and its position are set at the
    same wizard step, so a node with no position has no name for one either.
    v1.2.2 made it nullable for exactly that reason, and the null now says "this
    node cannot name its illuminator" rather than a placeholder saying something
    no owner chose.

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
