"""Tests for building two models at the same time (ADR 0040).

The wait before a user can speak is two waits — a recogniser and a translation model, neither
needing the other — and in series it measured 8.1 s to first text on CTranslate2 and 21.3 s on
ONNX with a bridged pair, against a 3 s target and a 6 s hard limit.

What matters here is not that it is faster: that is measured in `docs/PERFORMANCE_BUDGET.md`
and depends on the machine. What matters is that the two calls actually overlap, that both
results come back in the order they were asked for, and that a failure in either is reported
rather than swallowed by a thread nobody is watching.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

from on_the_fly.infrastructure import parallel


def test_both_results_come_back_in_the_order_they_were_asked_for() -> None:
    first, second = parallel.both(lambda: "recogniser", lambda: 42)

    assert first == "recogniser"
    assert second == 42


def test_the_two_calls_actually_overlap() -> None:
    """The whole point. Each waits for the other to have started, so a serial implementation
    deadlocks on the barrier rather than passing slowly."""
    started = threading.Barrier(2, timeout=5)

    def work(value: str) -> str:
        started.wait()
        return value

    assert parallel.both(lambda: work("a"), lambda: work("b")) == ("a", "b")


def test_the_wait_is_the_longer_of_the_two_and_not_their_sum() -> None:
    def sleeper(seconds: float) -> float:
        time.sleep(seconds)
        return seconds

    began = time.monotonic()
    parallel.both(lambda: sleeper(0.20), lambda: sleeper(0.20))
    elapsed = time.monotonic() - began

    assert elapsed < 0.35, f"the two calls did not overlap: {elapsed:.2f}s"


def test_a_failure_in_the_first_is_raised() -> None:
    def fails() -> None:
        raise RuntimeError("the recogniser could not be loaded")

    with pytest.raises(RuntimeError, match="recogniser"):
        parallel.both(fails, lambda: "translator")


def test_a_failure_in_the_second_is_raised() -> None:
    def fails() -> None:
        raise RuntimeError("no pinned translation model")

    with pytest.raises(RuntimeError, match="no pinned translation model"):
        parallel.both(lambda: "recogniser", fails)


def test_when_both_fail_the_first_one_asked_for_is_the_one_reported() -> None:
    """A caller who asked for a recogniser and a translator wants to know that the recogniser
    could not be loaded, not whichever thread happened to finish first."""

    def recogniser() -> None:
        raise RuntimeError("recogniser")

    def translator() -> None:
        raise RuntimeError("translator")

    with pytest.raises(RuntimeError, match=r"^recogniser$"):
        parallel.both(recogniser, translator)


def test_the_other_call_is_finished_before_a_failure_returns() -> None:
    """No thread outlives this holding a half-built model. A model still loading while the
    command line prints an error and exits is a file handle and 400 MB nobody is tracking."""
    finished = threading.Event()

    def slow() -> str:
        time.sleep(0.1)
        finished.set()
        return "done"

    def fails() -> None:
        raise RuntimeError("immediately")

    with pytest.raises(RuntimeError, match="immediately"):
        parallel.both(fails, slow)

    assert finished.is_set(), "the second call was abandoned rather than waited for"


def test_a_result_of_none_is_a_result() -> None:
    """`load_translator` returns `None` when there is nothing to translate into, and that is
    an answer rather than a failure."""
    assert parallel.both(lambda: "recogniser", lambda: None) == ("recogniser", None)


# --------------------------------------------------------------------------------------
# `each`, which digests a model's files. Same discipline, any number of calls.
# --------------------------------------------------------------------------------------


def test_results_come_back_in_the_order_they_were_given() -> None:
    """Not in completion order. `verify` zips these against the pin's declared digests, so
    a result landing in the wrong slot would compare a file against another file's hash."""

    def after(seconds: float, value: str) -> Callable[[], str]:
        def work() -> str:
            time.sleep(seconds)
            return value

        return work

    assert parallel.each([after(0.15, "a"), after(0.0, "b"), after(0.05, "c")]) == ["a", "b", "c"]


def test_nothing_to_do_is_not_an_error() -> None:
    assert parallel.each([]) == []


def test_one_call_needs_no_threads() -> None:
    assert parallel.each([lambda: "only"]) == ["only"]


def test_all_of_them_overlap() -> None:
    started = threading.Barrier(3, timeout=5)

    def work(value: int) -> int:
        started.wait()
        return value

    assert parallel.each([lambda: work(1), lambda: work(2), lambda: work(3)]) == [1, 2, 3]


def test_the_earliest_failure_in_the_given_order_is_the_one_raised() -> None:
    """A model with two altered files should be reported by the name a reader would look at
    first, not by whichever thread finished first."""

    def fails(name: str) -> Callable[[], str]:
        def work() -> str:
            raise RuntimeError(name)

        return work

    with pytest.raises(RuntimeError, match=r"^encoder$"):
        parallel.each([lambda: "ok", fails("encoder"), fails("decoder")])


def test_every_call_finishes_before_a_failure_returns() -> None:
    finished = threading.Event()

    def slow() -> str:
        time.sleep(0.1)
        finished.set()
        return "done"

    def fails() -> str:
        raise RuntimeError("immediately")

    with pytest.raises(RuntimeError, match="immediately"):
        parallel.each([fails, slow])

    assert finished.is_set()
