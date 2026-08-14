"""``HostSnapshot`` and friends → ``HeartbeatRequest``.

The half of the service that runs unconditionally: from process start to
shutdown, whether or not detections are flowing and whether or not the last
frame was empty.

Only ``state``, ``uptime_s`` and ``config_version`` are required. Everything
else is optional throughout, which this module uses deliberately — **an absent
field is more honest than a guessed one**, and it is the only way to say "I do
not know" about a value the spec has no vocabulary for.
"""

from __future__ import annotations

from retina_telemetry.collect.host import HostSnapshot
from retina_telemetry.wire.models import (
    Adsb,
    Blah2,
    HeartbeatRequest,
    NodeHealth,
    NodeState,
    NodeVersions,
)


def build_heartbeat(
    *,
    state: NodeState,
    uptime_s: int,
    config_version: int | None,
    boot_id: str,
    host: HostSnapshot | None = None,
    blah2_up: bool | None = None,
    adsb_present: bool | None = None,
    owl_os: str | None = None,
    retina_node: str | None = None,
    blah2_image: str | None = None,
    errors: list[str] | None = None,
) -> HeartbeatRequest:
    """Assemble one heartbeat.

    | Wire field | Source |
    |---|---|
    | ``state`` | ``comms/lifecycle.py`` (stage 3b) |
    | ``uptime_s`` | see below |
    | ``config_version`` | ``state.py``, server-issued, ``null`` until issued |
    | ``boot_id`` | ``state.py``, distinct per process start |
    | ``health.cpu_pct`` | ``host.cpu_pct`` |
    | ``health.disk_free_mb`` | ``host.disk_free_mb`` |
    | ``health.temp_c`` | ``host.temp_c`` |
    | ``health.blah2`` | ``blah2_up`` |
    | ``health.adsb`` | ``adsb_present`` |
        | ``versions.*`` | compose env vars, read by ``__main__`` |
    | ``errors`` | ``errors.py`` |

    Args:
        state: the node's own account of itself. The server does not trust it —
            a node claiming to stream while no frames arrive is flagged using
            the server's own record of arrivals — so this is diagnostic.
        uptime_s: **the device's**, from ``HostSnapshot.host_uptime_s``.

            The spec does not say whose, and three candidates exist: the board's
            (``/proc/uptime``), blah2's (``/api/timing`` reports its own), and
            this process's. ``HeartbeatRequest`` is the node's account of
            itself and "the node" is the board, so a board that reboots
            repeatedly is the signal worth carrying. This process's uptime is a
            self-diagnostic and belongs in the status document, beside the
            ``seq`` discontinuity it explains.

            It is required, while ``host_uptime_s`` is best-effort like every
            other host read, so **the caller must resolve** a ``None``. Falling
            back to this process's uptime is correct rather than a fudge: the
            board has necessarily been up at least as long as we have, so it is
            a true lower bound. In practice ``/proc/uptime`` exists on every
            Linux and ``/proc`` is mounted in every container by default, so
            this path should never fire.
        config_version: server-issued, cached in ``state.py``, and ``None``
            until the first one arrives. Nullable on the wire since v1.1.1, so a
            node that has never held a version still beats and says so — see It remains required and non-null on ``DetectionFrame``.
        host: from ``collect.host.HostReader.read``. ``None``, or any ``None``
            field within it, omits the corresponding health entry. Note
            ``cpu_pct`` is always ``None`` on the first read, since ``/proc/stat``
            is cumulative and a percentage needs two samples.
        blah2_up: ``Blah2Client.last_poll_ok``. ``None`` before the first poll,
            which omits the field rather than guessing.
        adsb_present: whether the last ``DetectionPoll`` carried an ``adsb`` key.
            Since blah2-api gates that key entirely on ``truth.adsb.enabled``,
            its presence *is* the config flag. Absent means ADS-B is off, which
            is not the same as broken, and the spec has no vocabulary for
            "disabled" — so the field is omitted rather than reported ``"down"``.
        owl_os, retina_node, blah2_image: image and OS versions. Omitted when
            unknown; ``retina_node`` currently has no readable source at all.
        errors: bounded list accumulated since the last beat, cleared once a
            beat is acknowledged.

    ``queue_depth`` no longer exists. It was meaningless under a
    transport with at most one request in flight and no queue — 0 and 1 the only
    reachable values, neither saying anything — and v1.1.1 removed it.
    """
    return HeartbeatRequest(
        state=state,
        uptime_s=uptime_s,
        config_version=config_version,
        boot_id=boot_id,
        health=_health(host, blah2_up, adsb_present),
        versions=_versions(owl_os, retina_node, blah2_image),
        errors=list(errors) if errors else [],
    )


def _health(
    host: HostSnapshot | None,
    blah2_up: bool | None,
    adsb_present: bool | None,
) -> NodeHealth:
    """Build the health block. Always present since v1.1.1.

    The four core fields are now **required and nullable**, so a value the node
    tried to read and could not arrives as an explicit ``null`` rather than as a
    missing key. That is the spec's own rule — ``null`` means *known to be
    unknown*, and absence is not left carrying meaning — and it is why this
    returns a block rather than ``None``: an all-null health block is a real
    report, not an empty one.

    ``adsb`` is the exception and stays optional, because its absence carries a
    third meaning the enum has no word for. blah2-api gates the key entirely on
    ``truth.adsb.enabled``, so a missing key means association is switched off.
    Reporting that as ``"down"`` would present a deliberate configuration as a
    fault; ``"unknown"`` would claim we looked and could not tell. The spec
    agrees a ``disabled`` value would say it better and holds that open.
    """
    return NodeHealth(
        cpu_pct=host.cpu_pct if host else None,
        disk_free_mb=host.disk_free_mb if host else None,
        temp_c=host.temp_c if host else None,
        blah2=None if blah2_up is None else (Blah2.up if blah2_up else Blah2.down),
        adsb=Adsb.up if adsb_present else None,
    )


def _versions(
    owl_os: str | None,
    retina_node: str | None,
    blah2_image: str | None,
) -> NodeVersions | None:
    versions = NodeVersions(owl_os=owl_os, retina_node=retina_node, blah2_image=blah2_image)
    return versions if versions.model_dump(exclude_none=True) else None
