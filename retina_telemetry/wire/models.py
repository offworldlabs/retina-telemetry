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
    version: Annotated[str, Field(max_length=32, title="Version")]
    accepted_at: Annotated[AwareDatetime, Field(title="Accepted At")]


class AdsbTag(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    hex: Annotated[str, Field(pattern="^[0-9a-f]{6}$", title="Hex")]
    lat: Annotated[float, Field(ge=-90.0, le=90.0, title="Lat")]
    lon: Annotated[float, Field(ge=-180.0, le=180.0, title="Lon")]
    alt: Annotated[float | None, Field(title="Alt")] = None
    gs: Annotated[float | None, Field(title="Gs")] = None
    track: Annotated[float | None, Field(title="Track")] = None
    expected_delay: Annotated[float | None, Field(title="Expected Delay")] = None
    expected_doppler: Annotated[float | None, Field(title="Expected Doppler")] = None
    delay_residual: Annotated[float | None, Field(title="Delay Residual")] = None
    doppler_residual: Annotated[float | None, Field(title="Doppler Residual")] = None


class ConfigResponse(BaseModel):
    config_version: Annotated[int, Field(ge=1, title="Config Version")]


class DetectionAck(BaseModel):
    accepted: Annotated[int, Field(ge=0, title="Accepted")]
    config_stale: Annotated[bool, Field(title="Config Stale")]
    streaming_allowed: Annotated[bool, Field(title="Streaming Allowed")]


class AdsbHexItem(RootModel[str | None]):
    root: Annotated[str | None, Field(pattern="^[0-9a-f]{6}$")]


class DetectionFrame(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    t: Annotated[float, Field(ge=0.0, title="T")]
    seq: Annotated[int, Field(ge=0, title="Seq")]
    boot_id: Annotated[str, Field(pattern="^[0-9a-z]{8,32}$", title="Boot Id")]
    config_version: Annotated[int, Field(ge=1, title="Config Version")]
    delay: Annotated[list[float], Field(max_length=512, title="Delay")]
    doppler: Annotated[list[float], Field(max_length=512, title="Doppler")]
    snr: Annotated[list[float], Field(max_length=512, title="Snr")]
    adsb_hex: Annotated[
        list[AdsbHexItem | None] | None, Field(deprecated=True, max_length=512, title="Adsb Hex")
    ] = None
    adsb: Annotated[list[AdsbTag | None] | None, Field(max_length=512, title="Adsb")] = None


class ErrorBody(BaseModel):
    error: Annotated[str, Field(max_length=64, title="Error")]
    detail: Annotated[str | None, Field(max_length=512, title="Detail")] = None


class State(StrEnum):
    starting = "starting"
    streaming = "streaming"
    stalled = "stalled"
    paused = "paused"
    error = "error"
    stopping = "stopping"


class Error(RootModel[str]):
    root: Annotated[str, Field(max_length=512)]


class NodeClaimRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    email: Annotated[
        str,
        Field(
            description="The owner's address, lower cased and trimmed by the server.",
            max_length=255,
        ),
    ]


class NodeConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    rx_lat: Annotated[float | None, Field(ge=-90.0, le=90.0)]
    rx_lon: Annotated[float | None, Field(ge=-180.0, le=180.0)]
    rx_alt_ft: Annotated[float | None, Field(ge=-1500.0, le=30000.0)]
    tx_lat: Annotated[float | None, Field(ge=-90.0, le=90.0)]
    tx_lon: Annotated[float | None, Field(ge=-180.0, le=180.0)]
    tx_alt_ft: Annotated[float | None, Field(ge=-1500.0, le=30000.0)]
    fc_hz: Annotated[float, Field(ge=1000000.0, le=6000000000.0)]
    fs_hz: Annotated[float, Field(ge=100000.0, le=20000000.0)]
    max_range_km: Annotated[float, Field(gt=0.0, le=1000.0)]
    cpi_s: Annotated[float, Field(gt=0.0, le=10.0)]
    delay_tolerance_us: Annotated[float, Field(gt=0.0)]
    doppler_tolerance_hz: Annotated[float, Field(gt=0.0)]
    tx_callsign: Annotated[str | None, Field(max_length=32, min_length=1)]
    beam_width_deg: Annotated[float | None, Field(gt=0.0, le=360.0)]
    beam_azimuth_deg: Annotated[float | None, Field(ge=0.0, lt=360.0)]


class NodeContact(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    first_name: Annotated[str | None, Field(max_length=64)] = None
    last_name: Annotated[str | None, Field(max_length=64)] = None
    email: Annotated[str | None, Field(max_length=255)] = None
    phone: Annotated[str | None, Field(max_length=32)] = None
    country: Annotated[str | None, Field(max_length=2, pattern="^[A-Za-z]{2}$")] = None


class Blah2(Enum):
    up = "up"
    down = "down"
    unknown = "unknown"


class Adsb(Enum):
    up = "up"
    down = "down"
    unknown = "unknown"


class NodeHealth(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    cpu_pct: Annotated[float | None, Field(ge=0.0, le=100.0, title="Cpu Pct")]
    disk_free_mb: Annotated[int | None, Field(ge=0, title="Disk Free Mb")]
    temp_c: Annotated[float | None, Field(ge=-50.0, le=150.0, title="Temp C")]
    blah2: Annotated[Blah2 | None, Field(title="Blah2")]
    adsb: Annotated[Adsb | None, Field(title="Adsb")] = None


class NodeVersions(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    owl_os: Annotated[str | None, Field(max_length=64, title="Owl Os")] = None
    retina_node: Annotated[str | None, Field(max_length=64, title="Retina Node")] = None
    blah2_image: Annotated[str | None, Field(max_length=64, title="Blah2 Image")] = None


class Choice(StrEnum):
    public = "public"
    private = "private"


class PublicationChoice(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    version: Annotated[str, Field(max_length=32, title="Version")]
    accepted_at: Annotated[AwareDatetime, Field(title="Accepted At")]
    choice: Annotated[Choice, Field(title="Choice")]


class RegisterResponse(BaseModel):
    token: Annotated[str, Field(max_length=128, min_length=32, title="Token")]
    node_ref: Annotated[str, Field(pattern="^(nde|sim)[0-9a-z]{12}$", title="Node Ref")]
    config_version: Annotated[int, Field(ge=1, title="Config Version")]
    server_time: Annotated[AwareDatetime, Field(title="Server Time")]


class ClaimState(StrEnum):
    unclaimed = "unclaimed"
    pending = "pending"
    owned = "owned"


class Agreements(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    licence: AcceptanceRecord
    remote_management: AcceptanceRecord
    publication: PublicationChoice


class ClaimResponse(BaseModel):
    state: ClaimState
    email: Annotated[str | None, Field(title="Email")]
    undeliverable: Annotated[bool, Field(title="Undeliverable")]


class ContactResponse(BaseModel):
    updated_at: Annotated[AwareDatetime, Field(title="Updated At")]
    claim_state: ClaimState
    claim_email: Annotated[str | None, Field(title="Claim Email")]
    claim_undeliverable: Annotated[bool, Field(title="Claim Undeliverable")]


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    state: Annotated[State, Field(title="State")]
    uptime_s: Annotated[int, Field(ge=0, title="Uptime S")]
    boot_id: Annotated[str, Field(pattern="^[0-9a-z]{8,32}$", title="Boot Id")]
    config_version: Annotated[int | None, Field(ge=1, title="Config Version")]
    health: NodeHealth | None = None
    versions: NodeVersions | None = None
    errors: Annotated[list[Error] | None, Field(max_length=32, title="Errors")] = None


class HeartbeatResponse(BaseModel):
    server_time: Annotated[AwareDatetime, Field(title="Server Time")]
    config_stale: Annotated[bool, Field(title="Config Stale")]
    streaming_allowed: Annotated[bool, Field(title="Streaming Allowed")]
    node_ref: Annotated[str, Field(pattern="^(nde|sim)[0-9a-z]{12}$", title="Node Ref")]
    claim_state: ClaimState
    claim_email: Annotated[str | None, Field(title="Claim Email")]
    claim_undeliverable: Annotated[bool, Field(title="Claim Undeliverable")]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )
    node_id: Annotated[str, Field(pattern="^ret[0-9a-f]{8}$", title="Node Id")]
    board_model: Annotated[str, Field(max_length=64, title="Board Model")]
    agreements: Agreements
    config: NodeConfig
