"""Host metrics — the one input that does not come from the node stack.

Three of these behave differently inside a container than the naive version
assumes, and two of the three fail silently if you get them wrong:

* ``/proc`` is **not** namespaced, so ``/proc/stat`` reports the host's CPU
  rather than this container's share. That is exactly what we want — ``cpu_pct``
  describes the node, and a node pegged by blah2 is the case worth reporting.
  Do not "fix" this by switching to cgroup accounting.
* ``statvfs`` **is** namespaced by mount, so calling it on ``/`` measures the
  container's overlay filesystem and tells you nothing. It must be called on a
  path that resolves to the node's real storage, which is why the default is
  ``/data``.
* ``/proc/uptime`` likewise gives *host* uptime, not this process's.

Every read is independently best-effort and yields ``None`` on failure. All of
them absent is a valid running state that still heartbeats.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PROC_STAT = Path("/proc/stat")
DEFAULT_PROC_UPTIME = Path("/proc/uptime")
DEFAULT_THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
DEFAULT_DISK_PATH = Path("/data")

#: Bit positions in ``vcgencmd get_throttled``. The low four are "right now";
#: the high four latch since boot, which is what catches a marginal PSU that
#: only browns out under load.
_THROTTLE_BITS = {
    "under_voltage_now": 0,
    "arm_freq_capped_now": 1,
    "throttled_now": 2,
    "soft_temp_limit_now": 3,
    "under_voltage_since_boot": 16,
    "arm_freq_capped_since_boot": 17,
    "throttled_since_boot": 18,
    "soft_temp_limit_since_boot": 19,
}


@dataclass(frozen=True)
class ThrottleFlags:
    raw: int
    under_voltage_now: bool
    arm_freq_capped_now: bool
    throttled_now: bool
    soft_temp_limit_now: bool
    under_voltage_since_boot: bool
    arm_freq_capped_since_boot: bool
    throttled_since_boot: bool
    soft_temp_limit_since_boot: bool

    @property
    def any_now(self) -> bool:
        return (
            self.under_voltage_now
            or self.arm_freq_capped_now
            or self.throttled_now
            or self.soft_temp_limit_now
        )

    @property
    def any_since_boot(self) -> bool:
        return (
            self.under_voltage_since_boot
            or self.arm_freq_capped_since_boot
            or self.throttled_since_boot
            or self.soft_temp_limit_since_boot
        )


@dataclass(frozen=True)
class HostSnapshot:
    cpu_pct: float | None
    temp_c: float | None
    disk_free_mb: int | None
    host_uptime_s: int | None
    throttle: ThrottleFlags | None


def parse_throttled(output: str) -> ThrottleFlags:
    """Parse ``throttled=0x50005`` into flags.

    Raises:
        ValueError: if the output is not in the expected form.
    """
    text = output.strip()
    _, _, value = text.partition("=")
    raw = int(value.strip(), 16)
    return ThrottleFlags(
        raw=raw, **{name: bool(raw & (1 << bit)) for name, bit in _THROTTLE_BITS.items()}
    )


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
        vcgencmd: str | None = "vcgencmd",
    ) -> None:
        self._proc_stat = Path(proc_stat)
        self._proc_uptime = Path(proc_uptime)
        self._thermal = Path(thermal)
        self._disk_path = Path(disk_path)
        self._vcgencmd = vcgencmd
        self._previous_cpu: tuple[int, int] | None = None

    def read(self) -> HostSnapshot:
        return HostSnapshot(
            cpu_pct=self._cpu_pct(),
            temp_c=self._temp_c(),
            disk_free_mb=self._disk_free_mb(),
            host_uptime_s=self._host_uptime_s(),
            throttle=self._throttle(),
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

    def _throttle(self) -> ThrottleFlags | None:
        """Pi throttle flags, or ``None`` where unavailable.

        Needs the Pi userland binary in the image *and* ``/dev/vcio`` mounted,
        which is a device mount on the one container that talks to the
        internet. Whether a sysfs route exists on current Pi kernels has not
        been checked on real hardware — until it is, treat this as optional.
        """
        if not self._vcgencmd or shutil.which(self._vcgencmd) is None:
            return None
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self._vcgencmd, "get_throttled"],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            return parse_throttled(completed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.debug("vcgencmd get_throttled failed: %s", exc)
            return None
