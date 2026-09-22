#!/usr/bin/env python3
"""Drive the claim endpoints by hand, against a real server.

**This is a test harness, not a feature.** `PUT /v1/nodes/claim` has no caller
in this service and deliberately so: nothing on a node knows the address that
owns it, and the contact email is a different question with a worse failure.
See `docs/data-sources.md` §4. What is missing is the trigger and the source of
the address, so this supplies both by hand and nothing else. Every request is
built and sent by the service's own code: `Client`, the generated
`NodeClaimRequest`, `to_wire`, and `apply_response`.

## It talks to the real server

There is no mock here. `offer` makes the server send somebody an email, and a
click on that link binds this node to the account behind the address. Releasing
it is the owner's to do from the dashboard and cannot be undone from the node.
So the two subcommands that cause a send both refuse to run without
`--confirm-send`, and `offer` additionally prints the address and makes you
name it again.

## Phases

    read          GET  /nodes/claim          safe, sends no mail
    offer         PUT  /nodes/claim          MAILS THE ADDRESS
    watch         GET  /nodes/claim, polled  safe, sends no mail
    offer-again   PUT  /nodes/claim          expects the 409, mails nothing
    resend        POST /nodes/claim/resend   MAILS THE ADDRESS AGAIN

Nothing here writes to disk. The token is read and never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from retina_telemetry.comms.client import DEFAULT_BASE_URL, Client, Outcome
from retina_telemetry.comms.levels import apply_response
from retina_telemetry.state import DEFAULT_TOKEN_PATH, State

CLAIM = "/nodes/claim"
RESEND = "/nodes/claim/resend"


def _report(outcome: Outcome, state: State) -> None:
    """What the server said, and what the node made of it."""
    print(f"  {outcome.kind.name} {outcome.status}")
    if outcome.body is not None:
        print(f"  body: {json.dumps(outcome.body, sort_keys=True)}")
    if outcome.error:
        print(f"  error: {outcome.error}")

    apply_response(outcome, state)

    snapshot = state.snapshot()
    print(f"  node now holds: {snapshot.claim}")
    # Proves the claim 409 is not mistaken for a configuration conflict. A
    # PUT /nodes/config on the wire here would be pure noise. Cleared at
    # startup in `_client`, so a True here was caused by this response.
    queued = state.config_resend.is_set()
    print(f"  config resend queued by this response: {queued}{'  <-- WRONG' if queued else ''}")


def _client(args: argparse.Namespace) -> tuple[Client, State, str]:
    state = State(args.token_path)
    snapshot = state.snapshot()
    if not snapshot.registered:
        sys.exit(f"no token at {args.token_path}: this node is not registered")
    # Loading a token always queues a configuration resend, because a restored
    # token never comes with a config_version. That is `State._load` doing its
    # job and has nothing to do with the claim, so it is cleared here: past
    # this point a queued resend means a *response* asked for one, which is
    # exactly what the claim 409 must not do.
    state.config_resend.clear()

    # Never the token itself. Its length is enough to say one was loaded.
    print(f"→ {args.api_url}")
    print(f"  token loaded from {args.token_path} ({len(snapshot.token)} chars)")
    print(f"  node_ref {snapshot.node_ref or 'unknown until a response carries it'}")
    return Client(base_url=args.api_url), state, snapshot.token


def cmd_read(args: argparse.Namespace) -> int:
    client, state, token = _client(args)
    print("→ GET /nodes/claim")
    # `None` rather than `{}`: requests sends no body at all, which is what a
    # GET should carry.
    _report(client.request("GET", CLAIM, None, token=token), state)
    return 0


def cmd_offer(args: argparse.Namespace) -> int:
    from retina_telemetry.wire.models import NodeClaimRequest
    from retina_telemetry.wire.serialise import to_wire

    if args.confirm_address != args.email:
        sys.exit(
            "refusing to send: --confirm-address must repeat --email exactly.\n"
            f"  --email           {args.email}\n"
            f"  --confirm-address {args.confirm_address}"
        )

    client, state, token = _client(args)
    # Built and validated by the generated model, so the spec's own 255-char
    # bound fires here rather than at the server.
    payload = to_wire(NodeClaimRequest(email=args.email))
    print(f"→ PUT /nodes/claim  {json.dumps(payload)}")
    print("  this sends mail")
    _report(client.request("PUT", CLAIM, payload, token=token), state)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    client, state, token = _client(args)
    print(f"→ polling GET /nodes/claim every {args.every}s for up to {args.seconds}s")
    print("  (the spec's own cadence while a setup page is open)")

    deadline = time.monotonic() + args.seconds
    last: str | None = None
    while time.monotonic() < deadline:
        outcome = client.request("GET", CLAIM, None, token=token)
        apply_response(outcome, state)
        claim = state.snapshot().claim
        current = repr(claim)
        if current != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {current}")
            last = current
        if claim is not None and claim.state == "owned":
            print("  owned. The link was clicked.")
            return 0
        time.sleep(args.every)

    print("  still not owned when time ran out.")
    return 1


def cmd_offer_again(args: argparse.Namespace) -> int:
    from retina_telemetry.wire.models import NodeClaimRequest
    from retina_telemetry.wire.serialise import to_wire

    client, state, token = _client(args)
    payload = to_wire(NodeClaimRequest(email=args.email))
    print(f"→ PUT /nodes/claim again  {json.dumps(payload)}")
    print("  expecting 409 with a ClaimResponse, and no configuration resend")
    _report(client.request("PUT", CLAIM, payload, token=token), state)
    return 0


def cmd_resend(args: argparse.Namespace) -> int:
    client, state, token = _client(args)
    print("→ POST /nodes/claim/resend")
    print("  this sends mail, unless the node is already owned or the address bounced")
    _report(client.request("POST", RESEND, None, token=token), state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token-path", type=Path, default=DEFAULT_TOKEN_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("read").set_defaults(run=cmd_read)

    offer = sub.add_parser("offer")
    offer.add_argument("--email", required=True)
    offer.add_argument(
        "--confirm-address",
        required=True,
        help="repeat --email exactly. A real person is mailed by this call.",
    )
    offer.add_argument("--confirm-send", action="store_true", required=True)
    offer.set_defaults(run=cmd_offer)

    watch = sub.add_parser("watch")
    watch.add_argument("--seconds", type=int, default=600)
    watch.add_argument("--every", type=int, default=5)
    watch.set_defaults(run=cmd_watch)

    again = sub.add_parser("offer-again")
    again.add_argument("--email", required=True)
    again.set_defaults(run=cmd_offer_again)

    resend = sub.add_parser("resend")
    resend.add_argument("--confirm-send", action="store_true", required=True)
    resend.set_defaults(run=cmd_resend)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    raise SystemExit(main())
