"""Host metrics — the one input that does not come from the node stack.

Exactly the four values the ingest spec asks for: ``cpu_pct``, ``temp_c`` and
``disk_free_mb`` from ``NodeHealth``, and uptime for ``HeartbeatRequest``.
Nothing else. Pi throttle flags were considered and dropped — the spec has no
field for them, so carrying them would have meant a ``/dev/vcio`` device mount
on the one container that talks to the internet, in exchange for data with
nowhere to go.

Three of these behave differently inside a container than the naive version
assumes, and two of the three fail silently if you get them wrong:

* ``/proc`` is **not** namespaced, so ``/proc/stat`` reports the host's CPU
  rather than this container's share. That is exactly what we want — ``cpu_pct``
  describes the node, and a node pegged by blah2 is the case worth reporting.
  Do not "fix" this by switching to cgroup accounting.
* ``statvfs`` **is** namespaced by mount, so calling it on ``/`` measures the
  container's overlay filesystem. It must be called on a path that resolves to
  the node's real storage, which is why the default is ``/data``.

  Beware of testing this on the current fleet: Docker's data-root is
  ``/data/docker``, so a container's overlay happens to sit on the very
  filesystem we want and ``statvfs("/")`` returns the right answer by accident.
  It is still wrong — move the data-root, or give a node a separate Docker
  partition, and it silently starts reporting something else.
* ``/proc/uptime`` likewise gives *host* uptime, not this process's.

Every read is independently best-effort and yields ``None`` on failure. All of
them absent is a valid running state that still heartbeats.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PROC_STAT = Path("/proc/stat")
DEFAULT_PROC_UPTIME = Path("/proc/uptime")
DEFAULT_THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
DEFAULT_DISK_PATH = Path("/data")


@dataclass(frozen=True)
class HostSnapshot:
    cpu_pct: float | None
    temp_c: float | None
    disk_free_mb: int | None
    host_uptime_s: int | None


class HostReader:
    """Collects host metrics, holding the previous CPU sample.

    ``/proc/stat`` reports cumulative jiffies since boot, so a percentage needs
    two samples and the interval between them. That makes this the only
    collection module that cannot answer from a single call: the first
    :meth:`read` returns ``cpu_pct=None`` and primes the sample.
    """

    def __init__(
        self,
        *,
        proc_stat: Path | str = DEFAULT_PROC_STAT,
        proc_uptime: Path | str = DEFAULT_PROC_UPTIME,
        thermal: Path | str = DEFAULT_THERMAL,
        disk_path: Path | str = DEFAULT_DISK_PATH,
    ) -> None:
        self._proc_stat = Path(proc_stat)
        self._proc_uptime = Path(proc_uptime)
        self._thermal = Path(thermal)
        self._disk_path = Path(disk_path)
        self._previous_cpu: tuple[int, int] | None = None

    def read(self) -> HostSnapshot:
        return HostSnapshot(
            cpu_pct=self._cpu_pct(),
            temp_c=self._temp_c(),
            disk_free_mb=self._disk_free_mb(),
            host_uptime_s=self._host_uptime_s(),
        )

    # ── individual reads, each best-effort ───────────────────────────

    def _cpu_pct(self) -> float | None:
        sample = self._read_cpu_sample()
        if sample is None:
            return None

        previous, self._previous_cpu = self._previous_cpu, sample
        if previous is None:
            return None  # first call primes the sample

        busy_delta = sample[0] - previous[0]
        total_delta = sample[1] - previous[1]
        if total_delta <= 0:
            return None  # counters reset, or two reads inside one jiffy
        return round(100.0 * busy_delta / total_delta, 1)

    def _read_cpu_sample(self) -> tuple[int, int] | None:
        """Return ``(busy_jiffies, total_jiffies)`` from the aggregate line."""
        try:
            first_line = self._proc_stat.read_text(encoding="utf-8").split("\n", 1)[0]
        except OSError as exc:
            log.debug("cannot read %s: %s", self._proc_stat, exc)
            return None

        fields = first_line.split()
        if not fields or fields[0] != "cpu" or len(fields) < 5:
            log.debug("unexpected first line in %s: %r", self._proc_stat, first_line)
            return None

        try:
            values = [int(field) for field in fields[1:]]
        except ValueError:
            log.debug("non-integer jiffies in %s: %r", self._proc_stat, first_line)
            return None

        # user nice system idle iowait irq softirq steal …
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total - idle, total

    def _temp_c(self) -> float | None:
        try:
            millidegrees = int(self._thermal.read_text(encoding="utf-8").strip())
        except (OSError, ValueError) as exc:
            log.debug("cannot read %s: %s", self._thermal, exc)
            return None
        return round(millidegrees / 1000.0, 1)

    def _disk_free_mb(self) -> int | None:
        try:
            stat = os.statvfs(self._disk_path)
        except OSError as exc:
            log.debug("cannot statvfs %s: %s", self._disk_path, exc)
            return None
        # f_bavail, not f_bfree: blocks available to unprivileged users is what
        # actually constrains us.
        return int(stat.f_bavail * stat.f_frsize / 1_000_000)

    def _host_uptime_s(self) -> int | None:
        try:
            first_field = self._proc_uptime.read_text(encoding="utf-8").split()[0]
            return int(float(first_field))
        except (OSError, ValueError, IndexError) as exc:
            log.debug("cannot read %s: %s", self._proc_uptime, exc)
            return None
