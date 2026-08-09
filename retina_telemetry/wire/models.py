# GENERATED FILE — do not edit by hand.
#
# Regenerate with tools/generate-models.sh after any change to
# docs/node-ingest-v1.yml. The spec is the contract; this is derived from it.

from __future__ import annotations

from typing import Annotated
from pydantic import AwareDatetime, BaseModel, Field


class Agreement(BaseModel):
    version: Annotated[str, Field(examples=["2026-07-01"])]
    accepted_at: Annotated[AwareDatetime, Field(examples=["2026-07-31T09:12:00Z"])]


class NodeConfig(BaseModel):
    rx_lat: Annotated[
        float,
        Field(
            description="Receiver latitude, degrees. Moving this by more than 0.005° rotates `node_ref`.",
            examples=[51.42],
        ),
    ]
    rx_lon: Annotated[
        float,
        Field(
            description="Receiver longitude, degrees. Moving this by more than 0.005° rotates `node_ref`.",
            examples=[-0.91],
        ),
    ]
    rx_alt_ft: Annotated[float, Field(description="Receiver altitude, feet.", examples=[120])]
    tx_lat: Annotated[float, Field(description="Illuminator latitude, degrees.", examples=[51.37])]
    tx_lon: Annotated[float, Field(description="Illuminator longitude, degrees.", examples=[-0.88])]
    tx_alt_ft: Annotated[float, Field(description="Illuminator altitude, feet.", examples=[900])]
    tx_callsign: Annotated[str, Field(description="Illuminator callsign.")]
    fc_hz: Annotated[float, Field(description="Centre frequency, Hz.", examples=[570000000])]
    fs_hz: Annotated[float, Field(description="Sample rate, Hz.", examples=[2000000])]
    beam_width_deg: Annotated[
        float, Field(description="Antenna beam width, degrees.", examples=[60])
    ]
    beam_azimuth_deg: Annotated[
        float | None,
        Field(
            description="Antenna boresight, degrees. `null` means broadside/omnidirectional; send `null` rather than\n`0.0` if you can.\n",
            examples=[None],
        ),
    ]
    max_range_km: Annotated[
        float, Field(description="Maximum range of interest, km.", examples=[150])
    ]


class RegisterRequest(BaseModel):
    node_id: Annotated[
        str,
        Field(
            description="Read from `/data/mender/node_id` on boot, never derived locally. `ret` plus eight hex\ncharacters.\n",
            examples=["ret1a2b3c4d"],
            pattern="^ret[0-9a-f]{8}$",
        ),
    ]
    board_model: Annotated[
        str, Field(description="Node-reported and diagnostic only.", examples=["raspberrypi5-4gb"])
    ]
    agreement: Agreement
    config: NodeConfig


class RegisterResponse(BaseModel):
    token: Annotated[
        str,
        Field(
            description="The bearer token. Persist it at mode 0600 under `/data`; losing it means re-registering,\nwhich needs an operator to open a reflash window.\n"
        ),
    ]
    node_ref: Annotated[
        str,
        Field(
            description="The node's public identifier, shown to the owner so they can find their data on the map. Cached\nfor display only, never sent back to the server, and it can rotate without warning.\n",
            examples=["nd4f2k9xq7m3b8vc"],
        ),
    ]
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on reflash\nrecovery the server already holds configuration history for that board, so the node's first\nversion after a reflash will not be 1.\n",
            examples=[7],
            ge=1,
        ),
    ]
    server_time: Annotated[
        AwareDatetime,
        Field(
            description="RFC 3339 UTC. It's there so the node can measure its clock offset and log a warning if it is\nlarge, since detection timestamps are node-clock and a Pi 5 has no battery-backed RTC.\n",
            examples=["2026-07-31T09:12:01Z"],
        ),
    ]


class DetectionFrame(BaseModel):
    t: Annotated[
        float,
        Field(
            description="Unix epoch seconds, node clock, the capture time of the CPI.",
            examples=[1753900000.123],
        ),
    ]
    seq: Annotated[
        int,
        Field(
            description="Per-node monotonic counter, incremented once per frame, for gap detection.",
            examples=[918273],
        ),
    ]
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on reflash\nrecovery the server already holds configuration history for that board, so the node's first\nversion after a reflash will not be 1.\n",
            examples=[7],
            ge=1,
        ),
    ]
    delay: Annotated[
        list[float],
        Field(
            description="Bistatic delay in microseconds, one per detection.",
            examples=[[12.4, 30.1]],
        ),
    ]
    doppler: Annotated[
        list[float],
        Field(description="Bistatic Doppler in Hz, one per detection.", examples=[[-118.0, 44.5]]),
    ]
    snr: Annotated[
        list[float], Field(description="SNR in dB, one per detection.", examples=[[14.2, 9.8]])
    ]
    adsb_hex: Annotated[
        list[str | None],
        Field(
            description="ICAO 24-bit hex of the associated aircraft, or `null` if unassociated. Association only:\nADS-B positions are better off elsewhere, on a separate lower-rate report which is not in\nv1. The field exists now so that the option survives.\n",
            examples=[["4ca1f2", None]],
        ),
    ]


class DetectionAck(BaseModel):
    accepted: Annotated[
        int,
        Field(
            description="Number of detections accepted. A mismatch against what was sent is worth logging.\n",
            examples=[2],
        ),
    ]
    config_stale: Annotated[
        bool,
        Field(
            description="The server's active `config_version` is not the one the node reported, so a `PUT /nodes/config`\nis due.\n"
        ),
    ]
    streaming_allowed: Annotated[
        bool,
        Field(
            description="While `false`, detections can pause while the heartbeat carries on, resuming once it goes\n`true`.\n"
        ),
    ]


class NodeHealth(BaseModel):
    cpu_pct: Annotated[float | None, Field(examples=[31])] = None
    disk_free_mb: Annotated[int | None, Field(examples=[9100])] = None
    temp_c: Annotated[float | None, Field(examples=[58])] = None
    blah2: Annotated[str | None, Field(examples=["up"])] = None
    adsb: Annotated[str | None, Field(examples=["up"])] = None
    queue_depth: Annotated[int | None, Field(examples=[0])] = None


class NodeVersions(BaseModel):
    owl_os: str | None = None
    retina_node: str | None = None
    blah2_image: str | None = None


class HeartbeatRequest(BaseModel):
    state: Annotated[
        str,
        Field(
            description="The node's own account of itself. The server does not trust it: a node reporting `streaming`\nwhile no frames have arrived is flagged as wedged, using the server's own record of frame\narrivals rather than anything in the health block.\n",
            examples=["streaming"],
        ),
    ]
    uptime_s: Annotated[int, Field(examples=[84213])]
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on reflash\nrecovery the server already holds configuration history for that board, so the node's first\nversion after a reflash will not be 1.\n",
            examples=[7],
            ge=1,
        ),
    ]
    health: NodeHealth | None = None
    versions: NodeVersions | None = None
    errors: Annotated[
        list[str] | None,
        Field(
            description="A bounded list accumulated since the last beat, not a single slot, so transient faults\nbetween beats are not lost. It can be cleared once a beat is acknowledged.\n",
            examples=[[]],
        ),
    ] = None


class HeartbeatResponse(BaseModel):
    server_time: Annotated[
        AwareDatetime,
        Field(
            description="RFC 3339 UTC. It's there so the node can measure its clock offset and log a warning if it is\nlarge, since detection timestamps are node-clock and a Pi 5 has no battery-backed RTC.\n",
            examples=["2026-07-31T09:12:01Z"],
        ),
    ]
    config_stale: Annotated[
        bool,
        Field(
            description="The server's active `config_version` is not the one the node reported, so a `PUT /nodes/config`\nis due.\n"
        ),
    ]
    streaming_allowed: Annotated[
        bool,
        Field(
            description="While `false`, detections can pause while the heartbeat carries on, resuming once it goes\n`true`.\n"
        ),
    ]
    node_ref: Annotated[
        str,
        Field(
            description="The node's public identifier, shown to the owner so they can find their data on the map. Cached\nfor display only, never sent back to the server, and it can rotate without warning.\n",
            examples=["nd4f2k9xq7m3b8vc"],
        ),
    ]


class ConfigResponse(BaseModel):
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on reflash\nrecovery the server already holds configuration history for that board, so the node's first\nversion after a reflash will not be 1.\n",
            examples=[7],
            ge=1,
        ),
    ]


class Error(BaseModel):
    error: Annotated[str, Field(examples=["forbidden"])]
    detail: str | None = None
