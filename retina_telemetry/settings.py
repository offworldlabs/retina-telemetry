"""Everything configurable, read from the environment once at startup.

Not from ``config.yml``. That file is retina-gui's, and putting our tunables in
it would invite the GUI to own them — it is mounted read-only here precisely
because this service is not its business. Compose already passes every other
service its settings this way.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from retina_telemetry.collect.blah2 import DEFAULT_BASE_URL as BLAH2_URL
from retina_telemetry.collect.consent import DEFAULT_CONSENT_PATH
from retina_telemetry.collect.host import DEFAULT_DISK_PATH
from retina_telemetry.collect.identity import DEFAULT_DEVICE_TYPE_PATH, DEFAULT_NODE_ID_PATH
from retina_telemetry.collect.node_config import DEFAULT_CONFIG_PATH
from retina_telemetry.comms.client import DEFAULT_BASE_URL as API_URL
from retina_telemetry.state import DEFAULT_TOKEN_PATH
from retina_telemetry.status import DEFAULT_STATUS_PATH


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default)


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    api_url: str = field(default_factory=lambda: os.environ.get("RETINA_API_URL") or API_URL)
    blah2_url: str = field(default_factory=lambda: os.environ.get("BLAH2_API_URL") or BLAH2_URL)

    token_path: Path = field(default_factory=lambda: _path("TOKEN_PATH", DEFAULT_TOKEN_PATH))
    status_path: Path = field(default_factory=lambda: _path("STATUS_PATH", DEFAULT_STATUS_PATH))
    node_id_path: Path = field(default_factory=lambda: _path("NODE_ID_PATH", DEFAULT_NODE_ID_PATH))
    device_type_path: Path = field(
        default_factory=lambda: _path("DEVICE_TYPE_PATH", DEFAULT_DEVICE_TYPE_PATH)
    )
    consent_path: Path = field(default_factory=lambda: _path("CONSENT_PATH", DEFAULT_CONSENT_PATH))
    config_path: Path = field(default_factory=lambda: _path("CONFIG_PATH", DEFAULT_CONFIG_PATH))
    disk_path: Path = field(default_factory=lambda: _path("DISK_PATH", DEFAULT_DISK_PATH))

    #: Faster than the producer so a CPI is rarely missed between polls. blah2
    #: emits roughly once a second on current hardware — slower than its
    #: configured CPI, because it is processing-bound.
    poll_interval_s: float = field(default_factory=lambda: _float("POLL_INTERVAL_S", 0.25))
    #: Server-issued in principle; nothing carries it yet.
    heartbeat_interval_s: float = field(
        default_factory=lambda: _float("HEARTBEAT_INTERVAL_S", 60.0)
    )
    #: How often to re-read config.yml looking for a local edit. The server can
    #: also ask, which arrives immediately via the resend event.
    config_poll_s: float = field(default_factory=lambda: _float("CONFIG_POLL_S", 30.0))
    status_interval_s: float = field(default_factory=lambda: _float("STATUS_INTERVAL_S", 10.0))

    #: Image tags, passed in by compose. retina_node has no readable source on
    #: a node today, so it is normally absent.
    owl_os: str | None = field(default_factory=lambda: os.environ.get("OWL_OS_V"))
    retina_node: str | None = field(default_factory=lambda: os.environ.get("RETINA_NODE_V"))
    blah2_image: str | None = field(default_factory=lambda: os.environ.get("BLAH2_V"))

    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL") or "INFO")
