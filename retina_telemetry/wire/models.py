# GENERATED FILE — do not edit by hand.
#
# Regenerate with tools/generate-models.sh after any change to
# docs/node-ingest-v1.yml. The spec is the contract; this is derived from it.

from __future__ import annotations

from typing import Annotated
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel
from enum import Enum, StrEnum


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
            description="The illuminator's name as the operator typed it in the tower step, free text and with\nspaces. Not a regulatory callsign, despite the field name: Tower-Finder holds those and\ncould be plumbed through instead, which is open. Any underscored form in an earlier\nexample was an example rather than a convention, so nothing normalises it.\n",
            examples=["Crystal Palace"],
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
        Field(
            description="Antenna beam width, degrees. `null` means the antenna has not been characterised, which is\nthe state of every node in the fleet: retina-gui does not collect the geometry from owners\nand is not scheduled to. Nullable rather than optional, on the same reasoning as `cpu_pct`\n— it is known to be unknown, and there should be exactly one way to say so.\n\nNothing is substituted for a missing value. A placeholder width would be wrong data the\nserver cannot detect, whereas `null` is a fact it can act on.\n",
            examples=[60],
            gt=0.0,
            le=360.0,
        ),
    ]
    beam_azimuth_deg: Annotated[
        float | None,
        Field(
            description="Antenna boresight, degrees. `null` means either broadside/omnidirectional or not\ncharacterised; the node cannot currently distinguish the two, since an unset configuration\nkey reads the same as a deliberate choice. Send `null` rather than `0.0` if you can.\n",
            examples=[None],
            ge=0.0,
            lt=360.0,
        ),
    ]
    max_range_km: Annotated[
        float,
        Field(
            description="Maximum range of interest, km. Derived on the node as\n`process.ambiguity.delayMax × c / fs / 1000` rather than read from a stored field, so it\ncan never disagree with what blah2 actually computes. Sending it rather than having the\nserver recompute it is deliberate: the derivation needs `delayMax`, which is not otherwise\non the wire, and the server is the compute-constrained end.\n",
            examples=[150],
            gt=0.0,
            le=1000.0,
        ),
    ]
    cpi_s: Annotated[
        float,
        Field(
            description="Coherent processing interval, seconds, from `process.data.cpi`. It is the width of the\ncapture window every `DetectionFrame.t` closes, so the server needs it to know what a\nframe's samples span, and it bounds how tightly two nodes' frames can be treated as\nsimultaneous.\n\nIt is not a send cadence. Processing takes longer than a CPI today, so frames arrive at\nroughly half this rate; `1 / cpi_s` is the ceiling on frame rate, never the expectation.\n",
            examples=[0.5],
            gt=0.0,
            le=10.0,
        ),
    ]
    delay_tolerance_us: Annotated[
        float,
        Field(
            description="The gate blah2-api applies when matching a detection to an ADS-B track, in the same unit\nas `DetectionFrame.delay`. It is node configuration, so strictness varies board to board,\nand without it `adsb_hex` values from two nodes are hypotheses formed under thresholds the\nserver cannot see and should not be compared as though they were alike.\n",
            examples=[6.67],
            gt=0.0,
        ),
    ]
    doppler_tolerance_hz: Annotated[
        float,
        Field(
            description="The Doppler half of the same gate, in Hz. See `delay_tolerance_us`.\n",
            examples=[5.0],
            gt=0.0,
        ),
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
            description="The Mender device type, from `/data/mender/device_type`. Node-reported and diagnostic only.\nMender targets artifacts by device type, so it is the string that decides which software a\nboard is allowed to receive, which makes it the more useful diagnostic than either a\nhardware description or `/proc/device-tree/model`, whose board revision means nothing to\neither end. It carries neither RAM size nor hardware revision; those would be separate\nfields if they are ever wanted.\n",
            examples=["pi5-v3-arm64"],
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
            description="Unix epoch seconds, node clock, the **end** of the capture window. blah2 stamps the clock\nthe moment the buffer holds a full CPI, before any processing, so the samples behind a\nframe span `[t - cpi_s, t]` and the frame itself arrives roughly a CPI-processing-time\nlater, about 900 ms today.\n\nThe window is described by this one number plus `cpi_s` in `NodeConfig` rather than by a\nstart and a duration on every frame, since the CPI is a configuration value that changes\nrarely and putting it on the hot path would repeat it at the frame rate.\n\nDetections are not individually timestamped within the window, and should not be. A CPI is\ncross-correlated as a whole and a detection is a peak in the resulting map, so its time\n*is* the window; the Doppler measurement is likewise an average over it. `t` plus `cpi_s`\nis the complete and honest description of when a detection happened, and it sets the floor\non how tightly two nodes' frames can meaningfully be called simultaneous.\n",
            examples=[1753900000.123],
            ge=0.0,
        ),
    ]
    seq: Annotated[
        int,
        Field(
            description="Counter incremented once per frame sent, from 0 at process start, for gap detection. It is\nrestart-local, so it must be read together with `boot_id`: same `boot_id` and a jump means\nframes were lost, a new `boot_id` means the node restarted.\n",
            examples=[918273],
            ge=0,
        ),
    ]
    boot_id: Annotated[
        str,
        Field(
            description="Distinct per process start, generated in memory and never persisted. It exists to make `seq`\ninterpretable: `seq` is restart-local and resets to 0, so without this the server cannot tell a\nreset from a gap. Persisting a monotonic counter instead would cost an fsync per frame on an SD\ncard, or be checkpointed coarsely enough to lie after a hard stop, and one write per boot is\nneither.\n\nThe pair `(boot_id, seq)` is what the server counts loss and staleness against, so it is\nrequired on every frame rather than on the heartbeat alone: a restart between two beats would\notherwise corrupt gap accounting for up to a minute. It need only be distinct, not ordered.\n",
            examples=["k3n8v2qp71ab"],
            pattern="^[0-9a-z]{8,32}$",
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


class Blah2(Enum):
    up = "up"
    down = "down"
    unknown = "unknown"
    NoneType_None = None


class Adsb(StrEnum):
    up = "up"
    down = "down"
    unknown = "unknown"


class NodeHealth(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    cpu_pct: Annotated[
        float | None,
        Field(
            description="`null` until two samples of `/proc/stat` exist, which means the first beat after every\nprocess start.\n",
            examples=[31],
            ge=0.0,
            le=100.0,
        ),
    ]
    disk_free_mb: Annotated[int | None, Field(examples=[9100], ge=0)]
    temp_c: Annotated[float | None, Field(examples=[58], ge=-50.0, le=150.0)]
    blah2: Annotated[
        Blah2 | None,
        Field(
            description='`null` before the first poll, meaning "we have not looked". `unknown` means "we looked and\ncould not tell", which is a different thing and does not currently arise on a node.\n',
            examples=["up"],
        ),
    ]
    adsb: Annotated[
        Adsb | None,
        Field(
            description='Omitted entirely when ADS-B is disabled in node configuration, since blah2-api gates the\nkey on `truth.adsb.enabled` and reporting a deliberate setting as a fault would page\nsomeone. Absence therefore means "disabled", not "unknown"; a dedicated `disabled` value\nwould say that better and is an open item rather than a change made here.\n',
            examples=["up"],
        ),
    ] = None


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
    stalled = "stalled"
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
    boot_id: Annotated[
        str,
        Field(
            description="Distinct per process start, generated in memory and never persisted. It exists to make `seq`\ninterpretable: `seq` is restart-local and resets to 0, so without this the server cannot tell a\nreset from a gap. Persisting a monotonic counter instead would cost an fsync per frame on an SD\ncard, or be checkpointed coarsely enough to lie after a hard stop, and one write per boot is\nneither.\n\nThe pair `(boot_id, seq)` is what the server counts loss and staleness against, so it is\nrequired on every frame rather than on the heartbeat alone: a restart between two beats would\notherwise corrupt gap accounting for up to a minute. It need only be distinct, not ordered.\n",
            examples=["k3n8v2qp71ab"],
            pattern="^[0-9a-z]{8,32}$",
        ),
    ]
    config_version: Annotated[
        int | None,
        Field(
            description="The node's active configuration version, or `null` when it does not yet hold one. Only the\nserver issues the value, from a registration or a `PUT /nodes/config` response, so there is\na window at every start where the node genuinely has none: the token is persisted across\nrestarts and the version deliberately is not, since caching it would let a node report a\nversion the server has since replaced.\n\n`null` rather than an absent key, so that the heartbeat really is unconditional. A node\nthat cannot build a configuration at all, and so can never PUT one, is precisely the node\nworth hearing from, and requiring a version here would make it the one that goes silent.\n\nIt stays required and non-null on `DetectionFrame`, where a frame cannot be filed without\nthe geometry it was measured against.\n",
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
