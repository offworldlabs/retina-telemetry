"""``NodeConfigRaw`` → ``NodeConfig``.

Sent in two places — inside ``RegisterRequest`` and on its own via
``PUT /nodes/config`` — so it is built once here and used by both.
"""

from __future__ import annotations

from retina_telemetry.collect.node_config import NodeConfigRaw
from retina_telemetry.wire.models import NodeConfig
from retina_telemetry.wire.units import m_to_ft, max_range_km


class IncompleteConfig(Exception):
    """The node cannot produce a configuration the server would accept.

    Raised rather than defaulted. A guessed beam width is worse than a node
    that will not register: the first is wrong data the server cannot detect,
    the second is a visible failure with a cause attached.
    """


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
    | ``beam_width_deg`` | ``config.beam_width_deg`` | none |
    | ``beam_azimuth_deg`` | ``config.beam_azimuth_deg`` | none |

    Two things worth knowing about the last two rows:

    * ``beam_width_deg`` does not exist on any node today (Q1), so this raises
      until retina-gui writes it. That is the intended failure — stage 1 reads
      it as optional because raising there would make every node unreadable,
      and this is the layer that knows the spec requires it.
    * ``beam_azimuth_deg`` of ``None`` is **not** missing. The spec asks for
      ``null`` rather than ``0.0`` to mean broadside/omnidirectional, so it is
      passed straight through.

    ``tx_callsign`` carries ``location.tx.name``, which is free text the
    operator typed in the tower step (e.g. "Crystal Palace") rather than a
    regulatory callsign. Q5 asks which the server wants.

    Raises:
        IncompleteConfig: if ``beam_width_deg`` is absent.
        ValueError: if ``fs_hz`` is not positive, via :func:`max_range_km`.
    """
    if config.beam_width_deg is None:
        raise IncompleteConfig(
            "beam_width_deg is required by the spec and is not configured on this node "
            "(open question Q1). Nothing in the node stack writes it yet, so registration "
            "and PUT /nodes/config cannot be built."
        )

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
    )
