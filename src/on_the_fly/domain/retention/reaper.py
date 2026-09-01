"""The thing that makes expiry happen without anyone asking.

Article 6 requires retention enforcement to be automatic. `EphemeralStore.reap()` does the
work but has to be called; this is what calls it. One daemon thread per store, sleeping on
a condition variable until the next deadline rather than polling, so an idle store costs
nothing and a due deadline is not waited out.

It is separated from the store so the store stays testable with no threads at all: tests
drive `reap()` against a `ManualClock` and assert exact behaviour, and this class is
tested for the narrower question of whether it wakes up and calls through.
"""

from __future__ import annotations

import threading
from types import TracebackType

from on_the_fly.domain.retention.clock import Clock, SystemClock
from on_the_fly.domain.retention.store import EphemeralStore, ReapReport

# An idle store still wakes occasionally, so a deadline added by another thread between a
# reap and a sleep cannot be missed indefinitely if a notify is ever lost.
IDLE_WAKE_SECONDS = 1.0


class ThreadedReaper:
    """Drives `EphemeralStore.reap()` on a background thread.

    Use as a context manager, or call `start()` and `stop()`. `stop()` performs a final
    reap so that shutdown does not strand content that was already past its deadline
    (handbook 35).
    """

    def __init__(
        self,
        store: EphemeralStore,
        *,
        clock: Clock | None = None,
        purge_on_stop: bool = False,
    ) -> None:
        self._store = store
        self._clock = clock if clock is not None else SystemClock()
        self._purge_on_stop = purge_on_stop
        self._wake = threading.Condition()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._last_report: ReapReport | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def last_report(self) -> ReapReport | None:
        """The most recent reap result, for diagnostics and tests."""
        return self._last_report

    def start(self) -> None:
        if self.running:
            return
        self._stopping = False
        # Daemon so a forgotten stop() cannot hang process exit. Correct shutdown still
        # goes through stop(), which reaps first.
        self._thread = threading.Thread(
            target=self._run, name=f"retention-reaper-{self._store.project_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the thread, reaping (or purging) once more on the way out."""
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

        if self._purge_on_stop:
            self._last_report = self._store.purge_all()
        else:
            self._last_report = self._store.reap()

    def notify(self) -> None:
        """Tell the reaper that deadlines may have changed.

        Optional. Missing a notify costs at most `IDLE_WAKE_SECONDS` of delay, never a
        missed deletion, because the loop re-reads the next deadline on every wake.
        """
        with self._wake:
            self._wake.notify_all()

    def __enter__(self) -> ThreadedReaper:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _run(self) -> None:
        while True:
            with self._wake:
                if self._stopping:
                    return
                timeout = self._sleep_seconds()
                self._wake.wait(timeout)
                if self._stopping:
                    return
            self._last_report = self._store.reap()

    def _sleep_seconds(self) -> float:
        """How long to wait before the next reap. Never negative, never unbounded."""
        deadline = self._store.next_deadline()
        if deadline is None:
            return IDLE_WAKE_SECONDS
        remaining = deadline - self._clock.now()
        if remaining <= 0:
            return 0.0
        return min(remaining, IDLE_WAKE_SECONDS)
