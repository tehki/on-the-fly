"""Time, as an injected dependency.

Retention deadlines are the one thing in this project that must be provably correct, and
a test that proves a ten-second deadline by sleeping for ten seconds is both slow and
flaky. Every component here takes a Clock, so tests drive time directly and deterministically
(handbook 18).

Monotonic time is used deliberately. Wall-clock time can jump backwards when a machine
syncs NTP or a user changes their timezone, and a deadline that moves backwards is a
deletion that silently does not happen.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """A source of monotonic time, in seconds."""

    def now(self) -> float:
        """Return the current monotonic time in seconds.

        Only differences between two readings are meaningful; the origin is arbitrary.
        """
        ...


class SystemClock:
    """The real clock. Monotonic, so it cannot be moved backwards by a clock adjustment."""

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()


class ManualClock:
    """A clock that only moves when a test moves it.

    This exists so retention behaviour can be asserted exactly: advance to one tick before
    a deadline and assert the content is still there, advance past it and assert it is gone.
    Neither assertion is possible against a real clock without sleeping and hoping.
    """

    __slots__ = ("_now",)

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        """Move time forward. Negative values are rejected; time does not run backwards."""
        if seconds < 0:
            raise ValueError("a clock cannot move backwards")
        self._now += seconds
        return self._now
