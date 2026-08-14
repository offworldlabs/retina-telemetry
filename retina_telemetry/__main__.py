"""Wiring, signals, and the five loops.

This is the only module that imports from all three layers, because assembling
them is precisely its job: ``collect`` reads the node, ``wire`` turns that into
payloads, ``comms`` sends them. Nothing here decides anything the other layers
have not already decided — if a rule lives here, it is in the wrong place.

## The loops guard themselves rather than being gated at startup

An earlier design had a supervisor withhold the threads until registration
succeeded. Guarding inside each loop turns out to be both simpler and more
robust: a node that becomes registered an hour later, or has its token revoked
and restored, needs no threads started or stopped, and there is no window where
a state change is missed because the supervisor was between checks.

Each loop is written to be safe when its preconditions do not hold:

    poll        always runs. Its liveness answer is worth having even when
                nothing can be sent, and it costs one local request.
    send        checks may_stream, and drains the slot regardless so a resumed
                node does not flush a stale frame.
    heartbeat   checks may_heartbeat.
    config      waits for the resend event, or notices a local edit.
    status      always runs. A node that can do nothing else must still say so.

## Nothing here retries

Retry policy is ``comms``'. These loops call into it and take whatever comes
back, which is what keeps the disciplines in one place.
"""

from __future__ import annotations

import contextlib
import logging
import random
import signal
import threading
from collections.abc import Callable
from typing import Any

import pydantic

from retina_telemetry.collect import consent as consent_reader
from retina_telemetry.collect import identity as identity_reader
from retina_telemetry.collect import node_config as config_reader
from retina_telemetry.collect.blah2 import Blah2Client
from retina_telemetry.collect.consent import Consent
from retina_telemetry.collect.host import HostReader
from retina_telemetry.collect.identity import IdentityUnavailable
from retina_telemetry.collect.node_config import ConfigUnavailable, NodeConfigRaw
from retina_telemetry.comms.client import Client
from retina_telemetry.comms.lifecycle import NodeState, Registrar, derive_state, explain
from retina_telemetry.comms.reliable import is_fatal_for_config, send_until_delivered
from retina_telemetry.comms.stream import DetectionStream, Slot
from retina_telemetry.errors import Errors
from retina_telemetry.settings import Settings
from retina_telemetry.state import State, with_uptime_fallback
from retina_telemetry.status import StatusWriter
from retina_telemetry.wire.config import build_node_config
from retina_telemetry.wire.detection import build_detection_frame
from retina_telemetry.wire.heartbeat import build_heartbeat
from retina_telemetry.wire.registration import IncompletePayload, build_registration
from retina_telemetry.wire.serialise import to_wire

log = logging.getLogger("retina_telemetry")


class Service:
    """Everything, assembled."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.stop = threading.Event()

        self.state = State(self.settings.token_path)
        self.errors = Errors()
        #: Loops that raised and will not come back. Reported in the status
        #: document, because a stopped loop is otherwise indistinguishable
        #: from a node that is simply quiet.
        self._dead_loops: set[str] = set()
        self.status = StatusWriter(self.settings.status_path)
        self.slot = Slot()

        self.blah2 = Blah2Client(self.settings.blah2_url)
        self.host = HostReader(disk_path=self.settings.disk_path)

        self.client = Client(self.settings.api_url)
        self.registrar = Registrar(self.client, self.state)
        self.stream = DetectionStream(self.client, self.state, self.slot)

        self._registering = threading.Event()
        # Two separate faults, because they clear on different events: a read
        # error goes away when the file becomes readable, a rejection only when
        # the server accepts a configuration. Sharing one field meant a
        # successful re-read wiped a rejection the operator still had to fix.
        self._config_unreadable: str | None = None
        self._config_rejected: str | None = None

    # ── node facts, re-read rather than cached ───────────────────────
    #
    # Cheap file reads, and re-reading means an operator fixing a problem takes
    # effect without a restart — which for the consent record is the difference
    # between opting in and having to bounce the container.

    def node_id(self) -> str | None:
        try:
            return identity_reader.read_node_id(self.settings.node_id_path)
        except IdentityUnavailable as exc:
            self.errors.add(f"identity: {exc}")
            return None

    def consent(self) -> Consent:
        return consent_reader.read_consent(self.settings.consent_path)

    def node_config(self) -> NodeConfigRaw | None:
        try:
            config = config_reader.read_config(self.settings.config_path)
        except ConfigUnavailable as exc:
            self._config_unreadable = str(exc)
            self.errors.add(f"config: {exc}")
            return None
        self._config_unreadable = None
        return config

    def current_state(self) -> NodeState:
        record = self.consent()
        return derive_state(
            self.state.snapshot(),
            has_identity=self.node_id() is not None,
            # The licence is what the spec says gates streaming; the other two
            # gate registration, and `complete` is what build_registration needs.
            licence_accepted=record.may_stream,
            all_records_present=record.complete,
            registering=self._registering.is_set(),
            detections_flowing=self.blah2.last_poll_ok,
            ever_detected=self.blah2.has_produced,
        )

    # ── the loops ────────────────────────────────────────────────────

    def poll_loop(self) -> None:
        """Poll blah2, build a frame, publish it. Always runs.

        ``seq`` is taken here rather than at send time on purpose: a gap in it
        should mean a frame existed and did not arrive, which is what the
        server reads it for.
        """
        while not self.stop.is_set():
            poll = self.blah2.poll_detection()
            if poll is not None:
                snapshot = self.state.snapshot()
                if snapshot.config_version is not None:
                    try:
                        frame = build_detection_frame(
                            poll,
                            seq=self.state.next_seq(),
                            boot_id=snapshot.boot_id,
                            config_version=snapshot.config_version,
                        )
                    except pydantic.ValidationError as exc:
                        # The spec bounds every field, so a value outside them
                        # now fails here rather than at the server. Dropping the
                        # frame is the discipline anyway; letting it escape
                        # would take the poll loop with it.
                        self.errors.add(f"frame rejected before sending: {exc.errors()[0]['msg']}")
                        log.warning("discarding an unsendable frame: %s", exc)
                    else:
                        self.slot.put(to_wire(frame))
            elif self.blah2.last_error:
                self.errors.add(self.blah2.last_error)
            self.stop.wait(self.settings.poll_interval_s)

    def send_loop(self) -> None:
        while not self.stop.is_set():
            if not self.stream.send_pending(timeout=self.settings.poll_interval_s) and (
                error := self.stream.last_error
            ):
                self.errors.add(error)

    def heartbeat_loop(self) -> None:
        # A uniform random phase offset within the interval, so that a fleet
        # restarting together does not settle into one bucket and post
        # simultaneously every minute. Same reasoning as the registration
        # jitter, and the spec asks for it explicitly.
        if self.stop.wait(random.uniform(0, self.settings.heartbeat_interval_s)):
            return
        while not self.stop.is_set():
            if self.state.snapshot().may_heartbeat:
                self._beat()
            self.stop.wait(self.settings.heartbeat_interval_s)

    def config_loop(self) -> None:
        """Resend on request, or when the file changes underneath us.

        Nothing pushes at us, so a local edit is noticed by re-reading. The
        server asking arrives immediately through the resend event.
        """
        last_sent: NodeConfigRaw | None = None
        while not self.stop.is_set():
            requested = self.state.config_resend.wait(self.settings.config_poll_s)
            if self.stop.is_set():
                return

            # Nothing to send with until registration has happened, and the
            # request would only earn a 401. The resend event stays set, so the
            # first pass after a token arrives sends immediately.
            if not self.state.snapshot().registered:
                continue

            config = self.node_config()
            if config is None:
                self.state.config_resend.clear()
                continue

            if (requested or config != last_sent) and self._send_config(config):
                last_sent = config

    def status_loop(self) -> None:
        """Always runs. A node that can do nothing else must still say so."""
        while not self.stop.is_set():
            self.write_status()
            self.stop.wait(self.settings.status_interval_s)

    def registration_loop(self) -> None:
        """Register once, then stop. Never re-registers.

        A revoked token is deliberately not a reason to come back here: that
        would turn one revocation into a registration storm.
        """
        while not self.stop.is_set():
            if self.state.snapshot().registered:
                return

            payload = self._registration_payload()
            if payload is None:
                # Blocked on something local — consent, identity, or the beam
                # fields. Re-checked rather than abandoned, so fixing it takes
                # effect without a restart.
                self.stop.wait(self.settings.config_poll_s)
                continue

            self._registering.set()
            try:
                outcome = self.registrar.attempt(payload)
            finally:
                self._registering.clear()

            if outcome.ok:
                return
            self.errors.add(outcome.describe())
            if self.stop.wait(self.registrar.delay_before_retry(outcome)):
                return

    # ── payloads ─────────────────────────────────────────────────────

    def _registration_payload(self) -> dict[str, Any] | None:
        node_id = self.node_id()
        config = self.node_config()
        if node_id is None or config is None:
            return None
        try:
            return to_wire(
                build_registration(
                    node_id=node_id,
                    board_model=identity_reader.read_board_model(self.settings.device_type_path),
                    consent=self.consent(),
                    config=config,
                )
            )
        except IncompletePayload as exc:
            log.info("cannot register yet: %s", exc)
            return None
        except ValueError as exc:
            # The same widening as _send_config, and it should have been done at
            # both call sites at once. pydantic's ValidationError is a
            # ValueError, so a config that parses but violates the spec's bounds
            # — a latitude past 90, an fc_hz below the minimum — used to escape
            # here and kill the registration loop for the lifetime of the
            # process, with nothing written anywhere to say why.
            self._config_rejected = f"configuration rejected: {exc}"
            self.errors.add(f"registration: {exc}")
            log.warning("cannot build a registration payload: %s", exc)
            return None

    def _beat(self) -> None:
        batch = self.errors.take()

        def payload() -> dict[str, Any] | None:
            # No config_version guard. It was here because the field was
            # required and non-null; v1.1.1 made it nullable precisely so a node
            # that has never held one still beats. Removing the guard is
            # the whole of that fix.
            snapshot = self.state.snapshot()
            host = self.host.read()
            return to_wire(
                build_heartbeat(
                    state=self.current_state().wire,
                    uptime_s=with_uptime_fallback(snapshot, host.host_uptime_s),
                    config_version=snapshot.config_version,
                    boot_id=snapshot.boot_id,
                    host=host,
                    blah2_up=self.blah2.last_poll_ok,
                    adsb_present=self.blah2.last_adsb_present,
                    owl_os=self.settings.owl_os,
                    retina_node=self.settings.retina_node,
                    blah2_image=self.settings.blah2_image,
                    errors=batch.messages,
                )
            )

        outcome = send_until_delivered(
            self.client,
            "POST",
            "/nodes/heartbeat",
            payload,
            state=self.state,
            stop=self.stop,
            token=self.state.snapshot().token,
            max_attempts=3,
        )
        if outcome is not None and outcome.ok:
            # Only now, and only what this beat carried. The spec is explicit
            # that the list is cleared once a beat is acknowledged.
            batch.commit()

    def _send_config(self, config: NodeConfigRaw) -> bool:
        try:
            payload = to_wire(build_node_config(config))
        except ValueError as exc:
            # Wider than the IncompleteConfig it replaces, and deliberately so.
            # pydantic's ValidationError is a ValueError, so this now also
            # catches a config that violates the spec's own bounds — a latitude
            # past 90, an fc_hz below the minimum. Those could previously kill
            # this loop, and a node that cannot report its configuration is
            # exactly the node worth hearing from.
            self._config_rejected = str(exc)
            self.errors.add(f"config: {exc}")
            self.state.config_resend.clear()
            return False

        outcome = send_until_delivered(
            self.client,
            "PUT",
            "/nodes/config",
            lambda: payload,
            state=self.state,
            stop=self.stop,
            token=self.state.snapshot().token,
        )
        if outcome is None:
            return False

        if outcome.ok:
            version = (outcome.body or {}).get("config_version")
            if isinstance(version, int):
                self.state.config_resent(version)
            self._config_rejected = None
            return True

        if is_fatal_for_config(outcome):
            # The node cannot stream at all until somebody edits the
            # configuration, so this has to reach the status document rather
            # than be retried into silence.
            self._config_rejected = f"the server rejected this configuration: {outcome.describe()}"
            self.errors.add(self._config_rejected)
            self.state.config_resend.clear()
        return False

    def write_status(self) -> None:
        state = self.current_state()
        self.status.write(
            state=str(state),
            snapshot=self.state.snapshot(),
            node_id=self.node_id(),
            detail=self._dead_loop_detail()
            or self._config_rejected
            or self._config_unreadable
            or explain(state),
            errors=self.errors.snapshot(),
        )

    def _dead_loop_detail(self) -> str | None:
        """Takes precedence over every other detail. A stopped loop means the
        node is not doing what the rest of the document claims it is."""
        if not self._dead_loops:
            return None
        return (
            f"internal fault: the {', '.join(sorted(self._dead_loops))} "
            f"loop{'s have' if len(self._dead_loops) > 1 else ' has'} stopped. "
            "Restart the container; this will not recover on its own."
        )

    # ── lifecycle ────────────────────────────────────────────────────

    def _supervised(self, name: str, loop: Callable[[], None]) -> Callable[[], None]:
        """Wrap a loop so an unexpected exception is loud rather than fatal.

        Every loop already guards the failures it expects. This is for the ones
        nobody thought of, and it exists because the alternative is silence: a
        daemon thread that raises writes nothing to ``errors[]``, nothing to the
        status document, and leaves the process alive with one fewer loop. A
        node that has stopped heartbeating looks exactly like a node that has
        lost power.

        Two real cases got here. A ``rx_lat`` outside the spec's bounds raised
        ``ValidationError`` out of the registration payload builder, and one
        fault longer than 508 characters seen twice rendered to 517 and failed
        ``HeartbeatRequest``'s ``maxLength``. Both killed their loop for the
        lifetime of the process; both are fixed at source, and this is what
        makes the next one visible instead.

        Restarting the loop is deliberately not attempted. A thread that died on
        a deterministic input would spin, and the useful outcome is that a human
        sees which loop stopped and why.
        """

        def supervised() -> None:
            try:
                loop()
            except BaseException as exc:  # noqa: BLE001 - the point is to catch everything
                self._dead_loops.add(name)
                self.errors.add(f"{name} loop stopped: {type(exc).__name__}: {exc}")
                log.exception("%s loop stopped and will not restart", name)
                # Written immediately rather than waiting for the status loop,
                # which may itself be the loop that died.
                with contextlib.suppress(Exception):
                    self.write_status()

        return supervised

    def run(self) -> int:
        log.info("starting; api=%s blah2=%s", self.settings.api_url, self.settings.blah2_url)
        self.write_status()

        threads = [
            threading.Thread(target=self._supervised(name, loop), name=name, daemon=True)
            for name, loop in (
                ("poll", self.poll_loop),
                ("send", self.send_loop),
                ("heartbeat", self.heartbeat_loop),
                ("config", self.config_loop),
                ("status", self.status_loop),
                ("registration", self.registration_loop),
            )
        ]
        for thread in threads:
            thread.start()

        self.stop.wait()
        log.info("stopping")

        for thread in threads:
            thread.join(timeout=5)
        self.blah2.close()
        self.write_status()
        return 0

    def shutdown(self, *_: Any) -> None:
        self.stop.set()


def main() -> int:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    service = Service(settings)
    for received in (signal.SIGTERM, signal.SIGINT):
        signal.signal(received, service.shutdown)
    return service.run()


if __name__ == "__main__":
    raise SystemExit(main())
