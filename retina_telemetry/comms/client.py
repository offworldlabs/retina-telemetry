"""One request, one answer about what happened.

This module performs HTTP and classifies the result. It does **not** decide
whether to retry, because the two traffic disciplines answer that differently:
registration, configuration and the heartbeat must land and so retry until they
do, while detections are latest-wins and a frame that fails is simply dropped in
favour of the next one. Putting policy here would force one of those to pretend
to be the other.

Everything shares one :class:`Client`, and therefore one kept-alive connection.
At 50 nodes and 2 Hz that is about 100 small requests per second against one
droplet, which is comfortable — but considerably cheaper without a TLS handshake
per frame.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.retina.fm/v1"

#: Connect and read timeouts. The spec wants a detection request abandoned
#: after "a few seconds" so the next frame goes out fresh; the must-land
#: endpoints override this with something longer.
DEFAULT_TIMEOUT_S = (3.05, 5.0)

USER_AGENT = "retina-telemetry"


class Kind(StrEnum):
    """What a response means, in the spec's own terms.

    Classified once here so no caller re-reads status codes, and so the two
    rules that are easy to get wrong live in one place: a 401 is not a reason
    to re-register, and a 409 is not a reason to retry the same frame.
    """

    OK = auto()
    #: 401 — token bad, revoked or expired. Stop streaming, keep heartbeating,
    #: and never re-register: that turns a deliberate revocation into a storm.
    UNAUTHORIZED = auto()
    #: 403 — registration refused, deliberately without saying why. Unknown
    #: device, not yet accepted by Mender, already holding a token, or in
    #: cooldown all look identical and answer at the same latency.
    REFUSED = auto()
    #: 400 — failed validation. Retrying unchanged will not help; surface it.
    INVALID = auto()
    #: 409 — the server does not recognise our config_version. PUT config,
    #: then resume.
    CONFLICT = auto()
    #: 429 — honour Retry-After. Skipped frames are dropped, not accumulated.
    RATE_LIMITED = auto()
    SERVER_ERROR = auto()
    #: No answer at all: connection refused, DNS failure, or our own timeout.
    UNREACHABLE = auto()


#: Kinds worth trying again as-is. A 409 is excluded on purpose — the same
#: request would fail identically until a configuration resend has happened.
_RETRYABLE = frozenset({Kind.REFUSED, Kind.RATE_LIMITED, Kind.SERVER_ERROR, Kind.UNREACHABLE})


@dataclass(frozen=True)
class Outcome:
    """What one request produced."""

    kind: Kind
    status: int | None
    body: dict[str, Any] | None
    retry_after_s: int | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.kind is Kind.OK

    @property
    def retryable(self) -> bool:
        """Whether repeating this exact request could succeed."""
        return self.kind in _RETRYABLE

    def describe(self) -> str:
        """A short line for ``errors[]`` and the status document.

        Never includes the request body, which for registration carries the
        agreement and for detections is large and uninteresting.
        """
        if self.error:
            return self.error
        return f"{self.kind} ({self.status})"


class Backoff:
    """Jittered exponential backoff.

    Jitter is not decoration here. Registration is rate-limited per node at
    5/hour and 20/day, and the spec notes that a CGNAT pool rebooting together
    is a case the server sizes for — so a fleet that retries in lockstep is the
    failure mode this exists to prevent.

    The jitter is ±50% around the exponential rather than the more common
    "full jitter" between zero and it, because a near-zero delay would burn the
    registration budget in minutes.
    """

    def __init__(
        self,
        *,
        base_s: float = 1.0,
        factor: float = 2.0,
        maximum_s: float = 300.0,
        jitter: float = 0.5,
        rng: random.Random | None = None,
    ) -> None:
        self._base_s = base_s
        self._factor = factor
        self._maximum_s = maximum_s
        self._jitter = jitter
        self._rng = rng or random.Random()
        self._attempts = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    def next_delay(self) -> float:
        """The next delay, advancing the sequence."""
        unjittered = min(self._base_s * (self._factor**self._attempts), self._maximum_s)
        self._attempts += 1
        low = unjittered * (1.0 - self._jitter)
        high = unjittered * (1.0 + self._jitter)
        return min(self._rng.uniform(low, high), self._maximum_s)

    def reset(self) -> None:
        self._attempts = 0


class Client:
    """A shared session, one kept-alive connection, no retry policy."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_s: tuple[float, float] | float = DEFAULT_TIMEOUT_S,
        session: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

        if session is None:
            import requests

            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
        self._session = session

    def post(self, path: str, payload: dict[str, Any], **kwargs: Any) -> Outcome:
        """Registration and detections. There is deliberately no ``put``: the
        only PUT we make is the configuration resend, and that goes through
        ``reliable.send_until_delivered``, which is generic over method."""
        return self.request("POST", path, payload, **kwargs)

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        token: str | None = None,
        timeout_s: tuple[float, float] | float | None = None,
    ) -> Outcome:
        """Send one request and classify the answer.

        Args:
            payload: already serialised with ``model_dump(mode="json",
                exclude_none=True)``. Passed as ``json=``
                so a required null survives — a payload built here would be a
                layering violation anyway.
            token: omitted for registration, which is what mints it.
            timeout_s: overrides the client default. The detection stream wants
                a short one so a slow request is abandoned and the next frame
                goes out fresh.
        """
        headers = {"Authorization": f"Bearer {token}"} if token else {}

        try:
            response = self._session.request(
                method,
                f"{self._base_url}/{path.lstrip('/')}",
                json=payload,
                headers=headers,
                timeout=timeout_s if timeout_s is not None else self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - no transport failure may escape
            # Deliberately broad: requests raises a family of exceptions for
            # DNS, connection, TLS and timeout, and every one of them means the
            # same thing to a caller — no answer, try later.
            return Outcome(
                kind=Kind.UNREACHABLE,
                status=None,
                body=None,
                retry_after_s=None,
                error=f"{method} {path} unreachable: {exc}",
            )

        return _classify(method, path, response)


def _classify(method: str, path: str, response: Any) -> Outcome:
    status = response.status_code
    body = _json_or_none(response)
    retry_after = _retry_after(response)

    if 200 <= status < 300:
        return Outcome(Kind.OK, status, body, retry_after, None)

    kind = {
        400: Kind.INVALID,
        401: Kind.UNAUTHORIZED,
        403: Kind.REFUSED,
        409: Kind.CONFLICT,
        429: Kind.RATE_LIMITED,
    }.get(status, Kind.SERVER_ERROR if status >= 500 else Kind.INVALID)

    detail = (body or {}).get("detail") or (body or {}).get("error") or ""
    error = f"{method} {path} → {status}" + (f": {detail}" if detail else "")
    return Outcome(kind, status, body, retry_after, error)


def _json_or_none(response: Any) -> dict[str, Any] | None:
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001 - an unparseable body is not an error here
        return None
    return parsed if isinstance(parsed, dict) else None


def _retry_after(response: Any) -> int | None:
    """Read ``Retry-After``, which the spec marks required on 403 and 429.

    Only the delta-seconds form is handled. The HTTP-date form is legal but the
    spec's own header schema is ``integer, minimum: 0``, so a date would be the
    server departing from its own contract — and guessing at it would be worse
    than falling back to our own backoff.
    """
    raw = (response.headers or {}).get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        log.warning("ignoring non-integer Retry-After: %r", raw)
        return None
    return max(seconds, 0)
