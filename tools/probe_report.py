"""Shared output helpers for the live probes.

Kept in one place so ``live-probe.sh`` can sum failures across probes into a
single exit code.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

_failures = 0


def failures() -> int:
    return _failures


def section(title: str) -> None:
    print(f"\n{BOLD}── {title} {'─' * max(0, 58 - len(title))}{RESET}")


def ok(label: str, value: object = "") -> None:
    print(f"  {GREEN}✓{RESET} {label}{f'  {DIM}{value}{RESET}' if value != '' else ''}")


def bad(label: str, value: object = "") -> None:
    global _failures
    _failures += 1
    print(f"  {RED}✗{RESET} {label}{f'  {DIM}{value}{RESET}' if value != '' else ''}")


def note(label: str, value: object = "") -> None:
    print(f"  {YELLOW}·{RESET} {label}{f'  {DIM}{value}{RESET}' if value != '' else ''}")


def detail(text: str) -> None:
    for line in text.splitlines():
        print(f"    {DIM}{line}{RESET}")


def check(label: str, condition: bool, value: object = "") -> None:
    (ok if condition else bad)(label, value)


def probe(title: str, fn: Callable[[], None]) -> None:
    section(title)
    try:
        fn()
    except Exception:
        bad(f"raised unexpectedly:\n{traceback.format_exc()}")


def summarise(title: str) -> int:
    print()
    if _failures:
        print(f"{RED}{BOLD}{title}: {_failures} check(s) failed{RESET}")
    else:
        print(f"{GREEN}{BOLD}{title}: all checks passed{RESET}")
    return _failures
