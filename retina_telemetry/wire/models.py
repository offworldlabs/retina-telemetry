# GENERATED FILE — do not edit by hand.
#
# Regenerate with tools/generate-models.sh after any change to
# docs/node-ingest-v1.yml. The spec is the contract; this is derived from it.

from __future__ import annotations

from typing import Annotated
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel
from enum import StrEnum


class AcceptanceRecord(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    version: Annotated[
        str,
        Field(
            description="The identifier of the text that was shown.",
            examples=["2026-07-01"],
            max_length=32,
        ),
    ]
    accepted_at: Annotated[AwareDatetime, Field(examples=["2026-07-31T09:12:00Z"])]


class Choice(StrEnum):
    public = "public"
    private = "private"


class PublicationChoice(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    version: Annotated[
        str,
        Field(
            description="The identifier of the disclosure text that was shown alongside the choice.",
            examples=["2026-07-01"],
            max_length=32,
        ),
    ]
    accepted_at: Annotated[AwareDatetime, Field(examples=["2026-07-31T09:12:00Z"])]
    choice: Annotated[Choice, Field(examples=["public"])]


class Agreements(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    licence: AcceptanceRecord
    remote_management: AcceptanceRecord
    publication: PublicationChoice


class NodeConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    rx_lat: Annotated[
        float, Field(description="Receiver latitude, degrees.", examples=[51.42], ge=-90.0, le=90.0)
    ]
    rx_lon: Annotated[
        float,
        Field(description="Receiver longitude, degrees.", examples=[-0.91], ge=-180.0, le=180.0),
    ]
    rx_alt_ft: Annotated[
        float, Field(description="Receiver altitude, feet.", examples=[120], ge=-1500.0, le=30000.0)
    ]
    tx_lat: Annotated[
        float,
        Field(description="Illuminator latitude, degrees.", examples=[51.37], ge=-90.0, le=90.0),
    ]
    tx_lon: Annotated[
        float,
        Field(description="Illuminator longitude, degrees.", examples=[-0.88], ge=-180.0, le=180.0),
    ]
    tx_alt_ft: Annotated[
        float,
        Field(description="Illuminator altitude, feet.", examples=[900], ge=-1500.0, le=30000.0),
    ]
    tx_callsign: Annotated[
        str,
        Field(
            description="Illuminator callsign.",
            examples=["CRYSTAL_PALACE"],
            max_length=32,
            min_length=1,
        ),
    ]
    fc_hz: Annotated[
        float,
        Field(
            description="Centre frequency, Hz.", examples=[570000000], ge=1000000.0, le=6000000000.0
        ),
    ]
    fs_hz: Annotated[
        float, Field(description="Sample rate, Hz.", examples=[2000000], ge=100000.0, le=20000000.0)
    ]
    beam_width_deg: Annotated[
        float | None,
        Field(description="Antenna beam width, degrees.", examples=[60], gt=0.0, le=360.0),
    ] = None
    beam_azimuth_deg: Annotated[
        float | None,
        Field(
            description="Antenna boresight, degrees. `null` means broadside/omnidirectional; send `null` rather than\n`0.0` if you can.\n",
            examples=[None],
            ge=0.0,
            lt=360.0,
        ),
    ] = None
    max_range_km: Annotated[
        float,
        Field(description="Maximum range of interest, km.", examples=[150], gt=0.0, le=1000.0),
    ]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    node_id: Annotated[
        str,
        Field(
            description="Read from `/data/mender/node_id` on boot, never derived locally. `ret` plus eight hex\ncharacters.\n",
            examples=["ret1a2b3c4d"],
            pattern="^ret[0-9a-f]{8}$",
        ),
    ]
    board_model: Annotated[
        str,
        Field(
            description="Node-reported and diagnostic only.",
            examples=["raspberrypi5-4gb"],
            max_length=64,
        ),
    ]
    agreements: Agreements
    config: NodeConfig


class RegisterResponse(BaseModel):
    token: Annotated[
        str,
        Field(
            description="The bearer token. Persist it at mode 0600 under `/data`; losing it means re-registering,\nwhich needs an operator to reactivate the node.\n",
            max_length=128,
            min_length=32,
        ),
    ]
    node_ref: Annotated[
        str,
        Field(
            description="The node's public identifier, shown to the owner so they can find their data on the map. `nde`\nfor a real node or `sim` for a synthetic one, then twelve lowercase alphanumeric characters\ndrawn from a CSPRNG, about 62 bits. Every `node_ref` is therefore exactly fifteen characters\nwhichever kind it is. Cached for display only, never sent back to the server, and it can rotate\nwithout warning.\n\nThe length is set by resistance to enumeration rather than by collision: the value is public,\nso the only thing guessing it buys is the ability to list nodes nobody has mentioned, and 62\nbits puts that far out of reach behind any rate limit. Collisions are irrelevant either way at\nthis fleet size.\n",
            examples=["nde4f2k9xq7m3b8"],
            pattern="^(nde|sim)[0-9a-z]{12}$",
        ),
    ]
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on operator\nreactivation the server already holds configuration history for that board, so the node's first\nversion afterwards will not be 1.\n",
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


class AdsbHexItem(RootModel[str | None]):
    root: Annotated[str | None, Field(pattern="^[0-9a-f]{6}$")]


class DetectionFrame(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    t: Annotated[
        float,
        Field(
            description="Unix epoch seconds, node clock, the capture time of the CPI.",
            examples=[1753900000.123],
            ge=0.0,
        ),
    ]
    seq: Annotated[
        int,
        Field(
            description="Per-node monotonic counter, incremented once per frame, for gap detection.",
            examples=[918273],
            ge=0,
        ),
    ]
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on operator\nreactivation the server already holds configuration history for that board, so the node's first\nversion afterwards will not be 1.\n",
            examples=[7],
            ge=1,
        ),
    ]
    delay: Annotated[
        list[float],
        Field(
            description="Bistatic delay in microseconds, one per detection.",
            examples=[[12.4, 30.1]],
            max_length=512,
        ),
    ]
    doppler: Annotated[
        list[float],
        Field(
            description="Bistatic Doppler in Hz, one per detection.",
            examples=[[-118.0, 44.5]],
            max_length=512,
        ),
    ]
    snr: Annotated[
        list[float],
        Field(description="SNR in dB, one per detection.", examples=[[14.2, 9.8]], max_length=512),
    ]
    adsb_hex: Annotated[
        list[AdsbHexItem | None],
        Field(
            description="ICAO 24-bit hex of the associated aircraft, or `null` if unassociated. Association only:\nADS-B positions are better off elsewhere, on a separate lower-rate report which is not in\nv1. The field exists now so that the option survives.\n",
            examples=[["4ca1f2", None]],
            max_length=512,
        ),
    ]


class DetectionAck(BaseModel):
    accepted: Annotated[
        int,
        Field(
            description="Number of detections accepted. In v1 the server accepts a frame whole or not at all, so\nthis always equals the length of the arrays; the field exists so that a later plausibility\ngate can accept fewer without a new response shape. A mismatch against what was sent is\nworth logging.\n",
            examples=[2],
            ge=0,
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


class Blah2(StrEnum):
    up = "up"
    down = "down"
    unknown = "unknown"


class Adsb(StrEnum):
    up = "up"
    down = "down"
    unknown = "unknown"


class NodeHealth(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    cpu_pct: Annotated[float | None, Field(examples=[31], ge=0.0, le=100.0)] = None
    disk_free_mb: Annotated[int | None, Field(examples=[9100], ge=0)] = None
    temp_c: Annotated[float | None, Field(examples=[58], ge=-50.0, le=150.0)] = None
    blah2: Annotated[Blah2 | None, Field(examples=["up"])] = None
    adsb: Annotated[Adsb | None, Field(examples=["up"])] = None
    queue_depth: Annotated[int | None, Field(examples=[0], ge=0)] = None


class NodeVersions(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    owl_os: Annotated[str | None, Field(max_length=64)] = None
    retina_node: Annotated[str | None, Field(max_length=64)] = None
    blah2_image: Annotated[str | None, Field(max_length=64)] = None


class NodeState(StrEnum):
    starting = "starting"
    streaming = "streaming"
    paused = "paused"
    error = "error"
    stopping = "stopping"


class Error(RootModel[str]):
    root: Annotated[str, Field(max_length=512)]


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    state: NodeState
    uptime_s: Annotated[int, Field(examples=[84213], ge=0)]
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on operator\nreactivation the server already holds configuration history for that board, so the node's first\nversion afterwards will not be 1.\n",
            examples=[7],
            ge=1,
        ),
    ]
    health: NodeHealth | None = None
    versions: NodeVersions | None = None
    errors: Annotated[
        list[Error] | None,
        Field(
            description="A bounded list accumulated since the last beat, not a single slot, so transient faults\nbetween beats are not lost. It can be cleared once a beat is acknowledged. Anything beyond\nthe bound is dropped node-side rather than truncating the request.\n",
            examples=[[]],
            max_length=32,
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
            description="The node's public identifier, shown to the owner so they can find their data on the map. `nde`\nfor a real node or `sim` for a synthetic one, then twelve lowercase alphanumeric characters\ndrawn from a CSPRNG, about 62 bits. Every `node_ref` is therefore exactly fifteen characters\nwhichever kind it is. Cached for display only, never sent back to the server, and it can rotate\nwithout warning.\n\nThe length is set by resistance to enumeration rather than by collision: the value is public,\nso the only thing guessing it buys is the ability to list nodes nobody has mentioned, and 62\nbits puts that far out of reach behind any rate limit. Collisions are irrelevant either way at\nthis fleet size.\n",
            examples=["nde4f2k9xq7m3b8"],
            pattern="^(nde|sim)[0-9a-z]{12}$",
        ),
    ]


class ConfigResponse(BaseModel):
    config_version: Annotated[
        int,
        Field(
            description="Server-owned version of the node's configuration. Returned rather than assumed: on operator\nreactivation the server already holds configuration history for that board, so the node's first\nversion afterwards will not be 1.\n",
            examples=[7],
            ge=1,
        ),
    ]


class Error1(BaseModel):
    error: Annotated[str, Field(examples=["forbidden"], max_length=64)]
    detail: Annotated[str | None, Field(max_length=512)] = None
