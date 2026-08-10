"""The one place shared state is touched.

Stages 1 and 2 are functions: call them, get an answer, nothing is shared.
Stage 3 introduces threads running at the same time — one polling blah2, one
sending, one heartbeating, one watching config — and a handful of facts they
all need. If two of them touch the same fact at the same moment you get bugs
that only appear under load and are miserable to reproduce, so everything
shared funnels through here and there is exactly one place to get it right.

## Reads are snapshots, not getters

There is no ``state.token`` property. Reading happens through :meth:`snapshot`,
which takes the lock once and returns a frozen view. Per-field getters would
each take the lock separately, so a caller reading three fields could see them
from three different moments — a token from before a re-registration and a
``config_version`` from after it, for instance. That class of bug is invisible
in review and impossible to reproduce on demand.

## The token is the only durable state

Two values come from the server rather than from the node: the ``token`` and
the ``config_version``. They differ in exactly one way, and it decides which
gets written to disk.

**The token has one source and it is operator-gated.** Only
``POST /nodes/register`` mints one, and for an identity the server has already
seen that succeeds only if an operator has opened a 24-hour reflash window.
Losing it strands the node until a human intervenes.

**``config_version`` has two sources and one of them is cheap.**
``PUT /nodes/config`` takes no ``config_version`` in its body and *returns* the
active one, so a node can recover a version by resending a configuration it
already has. Rate limits there are generous by design.

So the token is persisted and nothing else is. On start, a node holding a token
has no ``config_version`` and sets :attr:`config_resend`; the first
``PUT /nodes/config`` supplies one, and only then can it heartbeat or stream.

The accepted consequence: if ``config.yml`` is unreadable we cannot build a
``NodeConfig``, so we cannot PUT, so we never get a ``config_version``, so we
cannot heartbeat — ``HeartbeatRequest`` requires it. **A node with broken
configuration goes silent on the wire.** That is deliberate. It still says why
in the status document, which is the local half of the contract.

``seq`` is restart-local too, and for a different reason: persisting it would
cost an fsync per frame at 2 Hz on an SD card. Q10 proposes a ``boot_id`` to
make the discontinuity explicit; until the spec has a field for it we do not
invent one, and the node behaves identically either way.

## The token never appears in a log line or the status document

It is the only secret the node holds. :class:`Snapshot` carries it because the
sender needs it, but :meth:`redacted` exists for everything that reports.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

log = logging.getLogger(__name__)

#: Plain text, one line, because it holds one value. Under /data so it survives
#: an OS update, which is the whole reason it is durable.
DEFAULT_TOKEN_PATH = Path("/data/retina-telemetry/token")

#: Owner read/write only.
TOKEN_MODE = 0o600


@dataclass(frozen=True)
class Snapshot:
    """A consistent view of shared state, taken under one lock acquisition."""

    token: str | None
    node_ref: str | None
    config_version: int | None
    seq: int
    config_stale: bool
    streaming_allowed: bool
    token_rejected: bool
    clock_offset_s: float | None
    process_uptime_s: int

    @property
    def registered(self) -> bool:
        return self.token is not None

    @property
    def may_heartbeat(self) -> bool:
        """Whether a heartbeat can be built at all.

        Needs a token to authenticate and a ``config_version`` because the
        payload requires one. False on a fresh start until the first
        ``PUT /nodes/config`` lands.
        """
        return self.registered and self.config_version is not None

    @property
    def may_stream(self) -> bool:
        """Whether a detection frame may be posted right now.

        Everything the heartbeat needs, plus a token the server has not
        rejected and permission to stream.
        """
        return self.may_heartbeat and not self.token_rejected and self.streaming_allowed

    def redacted(self) -> dict[str, Any]:
        """Everything except the token, for logs and the status document."""
        return {
            "registered": self.registered,
            "node_ref": self.node_ref,
            "config_version": self.config_version,
            "seq": self.seq,
            "config_stale": self.config_stale,
            "streaming_allowed": self.streaming_allowed,
            "token_rejected": self.token_rejected,
            "clock_offset_s": self.clock_offset_s,
            "process_uptime_s": self.process_uptime_s,
        }


class State:
    """Shared state, guarded by a single lock.

    One lock rather than one per field: this is a handful of values touched a
    few times a second, so contention is irrelevant and a single lock is the
    version that is obviously correct.
    """

    def __init__(self, token_path: Path | str = DEFAULT_TOKEN_PATH) -> None:
        self._token_path = Path(token_path)
        self._lock = threading.Lock()
        self._started_at = monotonic()

        self._token: str | None = None
        self._node_ref: str | None = None
        self._config_version: int | None = None

        self._seq = 0
        self._config_stale = False
        self._streaming_allowed = True
        self._token_rejected = False
        self._clock_offset_s: float | None = None

        #: Set when a configuration resend is due — because the server said
        #: ``config_stale``, because a detection POST returned 409, or because
        #: we hold a token but no ``config_version``. The config watcher waits
        #: on it, so a resend happens promptly rather than at the next poll.
        self.config_resend = threading.Event()

        self._load()

    # ── reading ──────────────────────────────────────────────────────

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(
                token=self._token,
                node_ref=self._node_ref,
                config_version=self._config_version,
                seq=self._seq,
                config_stale=self._config_stale,
                streaming_allowed=self._streaming_allowed,
                token_rejected=self._token_rejected,
                clock_offset_s=self._clock_offset_s,
                process_uptime_s=int(monotonic() - self._started_at),
            )

    # ── writing ──────────────────────────────────────────────────────

    def next_seq(self) -> int:
        """Take the next frame counter. Restart-local and never persisted."""
        with self._lock:
            self._seq += 1
            return self._seq

    def store_token(self, token: str, *, node_ref: str, config_version: int) -> None:
        """Record a successful registration.

        The only method that writes to disk, and only the token goes there —
        ``node_ref`` and ``config_version`` are both re-obtainable from the
        server without an operator.
        """
        with self._lock:
            self._token = token
            self._node_ref = node_ref
            self._config_version = config_version
            self._token_rejected = False
            self._persist_token()

    def reject_token(self) -> None:
        """Record that the server refused our token.

        Does **not** clear it and must never trigger re-registration: treating a
        401 as a reason to register again turns one deliberate revocation into a
        registration storm. The token is kept so the failure stays visible and
        so a revocation that is later undone needs no operator action.
        """
        with self._lock:
            if not self._token_rejected:
                log.error("server rejected our token; stopping the stream, still heartbeating")
            self._token_rejected = True

    def apply_levels(
        self,
        *,
        config_version: int | None = None,
        config_stale: bool | None = None,
        streaming_allowed: bool | None = None,
        node_ref: str | None = None,
        server_time: datetime | None = None,
    ) -> None:
        """Adopt what a response carried, in one atomic step.

        Every response restates all of these, so they are levels rather than
        edges — repeated until the node notices. Applying them together means a
        reader can never see a new ``config_version`` alongside a stale
        ``config_stale``.

        Only the values actually present are applied; ``None`` means the
        response did not carry that field, which is not the same as false.
        Nothing here touches the disk.
        """
        with self._lock:
            if config_version is not None:
                self._config_version = config_version

            if node_ref is not None and node_ref != self._node_ref:
                # Display only, and it rotates when the receiver moves more than
                # 0.005°. Nothing functional depends on noticing.
                log.info("node_ref changed to %s", node_ref)
                self._node_ref = node_ref

            if streaming_allowed is not None:
                self._streaming_allowed = streaming_allowed

            if config_stale is not None:
                self._config_stale = config_stale

            if server_time is not None:
                self._clock_offset_s = (
                    datetime.now(UTC) - server_time.astimezone(UTC)
                ).total_seconds()

        # Outside the lock: waking a thread should not hold up other readers.
        if config_stale:
            self.config_resend.set()

    def request_config_resend(self) -> None:
        """Force a ``PUT /nodes/config``, for a 409 on a detection POST.

        A 409 says the server does not recognise our ``config_version``, which
        is the same condition ``config_stale`` reports, reached by a different
        route.
        """
        with self._lock:
            self._config_stale = True
        self.config_resend.set()

    def config_resent(self, config_version: int) -> None:
        """Record that the configuration landed and the server's active version.

        This is also how a freshly started node gets its first
        ``config_version``, which is why nothing can heartbeat until it happens.
        """
        with self._lock:
            self._config_version = config_version
            self._config_stale = False
        self.config_resend.clear()

    # ── the token on disk ────────────────────────────────────────────

    def _load(self) -> None:
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return  # an unregistered node, which is where every node starts
        except OSError as exc:
            log.error(
                "cannot read %s: %s — treating this node as unregistered", self._token_path, exc
            )
            return

        if not token:
            log.error("%s is empty — treating this node as unregistered", self._token_path)
            return

        self._token = token
        # A restored token never comes with a config_version, because we do not
        # persist one. The first PUT /nodes/config supplies it, and until then
        # this node can neither heartbeat nor stream.
        self.config_resend.set()

    def _persist_token(self) -> None:
        """Write atomically, so a crash mid-write cannot leave a torn token.

        Caller holds the lock.
        """
        try:
            self._token_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._token_path.with_name(self._token_path.name + ".tmp")
            # Created 0600 from the outset rather than chmod-ed afterwards,
            # which would leave the token world-readable for an instant.
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_MODE)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{self._token}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._token_path)
            os.chmod(self._token_path, TOKEN_MODE)
        except OSError as exc:
            # Never fatal. A node that cannot persist its token still works
            # until it restarts, and going silent would be worse.
            log.error("cannot write %s: %s", self._token_path, exc)

    def __repr__(self) -> str:
        """Redacted, so a stray repr in a log cannot leak the token."""
        return f"State({self.snapshot().redacted()})"


def with_uptime_fallback(snapshot: Snapshot, host_uptime_s: int | None) -> int:
    """Resolve ``HeartbeatRequest.uptime_s``.

    The device's uptime is what the field means, but it is a best-effort read
    while the field is required, so this falls back to our own. The board has
    necessarily been up at least as long as this process, making it a true
    lower bound rather than a fudge.
    """
    return host_uptime_s if host_uptime_s is not None else snapshot.process_uptime_s
