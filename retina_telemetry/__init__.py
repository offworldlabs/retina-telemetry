"""Node-side telemetry uplink for the RETINA passive radar fleet.

The build is staged by layer, not by endpoint:

    collect/   stage 1 — the interfaces to the rest of the node stack
    wire/      stage 2 — turning what we collected into wire payloads
    comms/     stage 3 — sending them, and everything that guards that

The invariant that keeps the layers honest: **stage 3 knows nothing about
radar, stage 1 knows nothing about the server.** Stage 2 is the only place
that knows both, and therefore the only place unit conversion happens.
"""
