"""Tests for runtime retention enforcement.

`CODING_AGENT_POLICY_v1.3-otf1.yaml` lists the tests this module is required to have, under
`retention.required_tests`. Rather than trusting that list to be honoured by hand, one
test in this file reads it and asserts a matching test function exists for every entry —
so adding a requirement to the policy fails the build until it is actually tested.

Everything here runs on a `ManualClock`. A retention test that sleeps is slow, flaky, and
proves less: with an injected clock we can assert the state one tick before a deadline and
one tick after, which is the behaviour that actually matters.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from on_the_fly.domain.retention import (
    DEFAULT_TRANSIENT_RETENTION_SECONDS,
    IDLE_WAKE_SECONDS,
    ContentExpiredError,
    EntryState,
    EphemeralStore,
    ManualClock,
    ProjectIsolationError,
    RecordingEventSink,
    RetentionConfigurationError,
    RetentionOverride,
    StoreCapacityExceededError,
    ThreadedReaper,
    TransientHandle,
    TransientRetentionPolicy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_FILE = REPO_ROOT / "CODING_AGENT_POLICY_v1.3-otf1.yaml"

SENSITIVE_TEXT = "the patient's diagnosis was confirmed on Tuesday"


class FakeDeleter:
    """A stand-in for a spill location, with controllable failure."""

    def __init__(self, location: str = "temp_directory", *, fail_times: int = 0) -> None:
        self._location = location
        self._fail_times = fail_times
        self.deleted: list[str] = []
        self.purge_calls = 0
        self.attempts = 0

    @property
    def location(self) -> str:
        return self._location

    def delete(self, entry_id: str) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise OSError("device busy")
        self.deleted.append(entry_id)

    def purge_all(self) -> None:
        self.purge_calls += 1


def make_store(
    clock: ManualClock,
    **kwargs: Any,
) -> EphemeralStore:
    return EphemeralStore("on-the-fly", clock=clock, **kwargs)


def active_override(seconds: float = 3600.0) -> RetentionOverride:
    issued = datetime(2026, 9, 1, tzinfo=UTC)
    return RetentionOverride(
        record_id="EXC-2026-09-01-003",
        owner="@tehki",
        reason="session transcript review for a specific accessibility trial",
        scope="transcripts in the accessibility trial build only",
        risk="MODERATE",
        approved_by="@tehki",
        compensating_controls=("opt-in per session", "local storage only"),
        issued_at=issued,
        expires_at=issued + timedelta(days=30),
        removal_condition="the trial ends",
        max_retention_seconds=seconds,
    )


# ======================================================================================
# The tests named by retention.required_tests in the policy.
# ======================================================================================


def test_default_10_second_expiry() -> None:
    """The default window is ten seconds, and it comes from the policy, not a guess."""
    clock = ManualClock()
    store = make_store(clock)
    assert store.retention_seconds == DEFAULT_TRANSIENT_RETENTION_SECONDS == 10.0

    handle = store.put(SENSITIVE_TEXT, label="speech_recognition_transcript")

    clock.advance(9.999)
    store.reap()
    assert store.is_present(handle), "content expired early"

    clock.advance(0.002)
    report = store.reap()
    assert not store.is_present(handle)
    assert report.deleted == (handle.entry_id,)
    assert report.ok


def test_automatic_expiry_without_followup_read() -> None:
    """Expiry does not wait to be asked.

    This is the case that matters most: content nobody ever looks at again. A store that
    expires lazily on read would keep this forever.
    """
    clock = ManualClock()
    store = make_store(clock)
    handle = store.put(b"\x00\x01audio", label="captured_audio_frames")

    clock.advance(11.0)
    # No read, no borrow, no is_present beforehand - only the clock-driven sweep.
    store.reap()

    assert len(store) == 0
    assert store.state_of(handle) is None


def test_post_use_refresh() -> None:
    """Continued legitimate use restarts the window rather than accumulating content."""
    clock = ManualClock()
    store = make_store(clock)
    handle = store.put(SENSITIVE_TEXT, label="translation_output")

    for _ in range(5):
        clock.advance(8.0)
        with store.borrow(handle) as content:
            assert content == SENSITIVE_TEXT
        store.reap()
        assert store.is_present(handle), "refreshed content should survive"

    # Once use genuinely stops, the window runs out from the end of the last use.
    clock.advance(10.001)
    store.reap()
    assert not store.is_present(handle)


def test_no_premature_deletion_during_active_use() -> None:
    """An open lease outlasts the deadline; the window restarts when use ends."""
    clock = ManualClock()
    store = make_store(clock)
    handle = store.put(SENSITIVE_TEXT, label="speech_recognition_transcript")

    with store.borrow(handle) as content:
        assert content == SENSITIVE_TEXT
        # A long transcription runs well past the window. Deleting here would pull the
        # buffer out from under its user.
        clock.advance(60.0)
        report = store.reap()
        assert report.deleted == ()
        assert store.is_present(handle)

    # Lease closed: the post-use clock starts now, not 60 seconds ago.
    store.reap()
    assert store.is_present(handle)

    clock.advance(10.001)
    store.reap()
    assert not store.is_present(handle)


def test_explicit_longer_retention_override() -> None:
    """Longer retention is possible only through a valid, active, sufficient exception."""
    now = datetime(2026, 9, 2, tzinfo=UTC)
    override = active_override(seconds=3600.0)

    policy = TransientRetentionPolicy.with_override(60.0, override, at=now)
    assert policy.seconds == 60.0

    clock = ManualClock()
    store = make_store(clock, policy=policy)
    handle = store.put(SENSITIVE_TEXT, label="session_caption_scrollback")

    clock.advance(30.0)
    store.reap()
    assert store.is_present(handle), "the override should permit 60s"

    clock.advance(30.001)
    store.reap()
    assert not store.is_present(handle), "even an override expires"

    # An override that has passed its own expiry authorises nothing.
    with pytest.raises(RetentionConfigurationError, match="not active"):
        TransientRetentionPolicy.with_override(
            60.0, override, at=override.expires_at + timedelta(seconds=1)
        )

    # An override cannot authorise more than it says it does.
    with pytest.raises(RetentionConfigurationError, match="permits at most"):
        TransientRetentionPolicy.with_override(7200.0, override, at=now)


def test_invalid_retention_configuration() -> None:
    """Bad configuration fails at construction, while someone is looking at it."""
    now = datetime(2026, 9, 2, tzinfo=UTC)

    with pytest.raises(RetentionConfigurationError, match="positive"):
        TransientRetentionPolicy(seconds=0)
    with pytest.raises(RetentionConfigurationError, match="positive"):
        TransientRetentionPolicy(seconds=-5)
    with pytest.raises(RetentionConfigurationError, match="finite"):
        TransientRetentionPolicy(seconds=float("inf"))

    # Longer than the default with no exception at all.
    with pytest.raises(RetentionConfigurationError, match="no override"):
        TransientRetentionPolicy(seconds=3600).validate(at=now)

    # An exception missing an Article 13 field is not an exception.
    with pytest.raises(RetentionConfigurationError, match="owner"):
        RetentionOverride(
            record_id="EXC-X",
            owner="   ",
            reason="r",
            scope="s",
            risk="LOW",
            approved_by="a",
            compensating_controls=("c",),
            issued_at=now,
            expires_at=now + timedelta(days=1),
            removal_condition="done",
            max_retention_seconds=60.0,
        )

    with pytest.raises(RetentionConfigurationError, match="compensating controls"):
        RetentionOverride(
            record_id="EXC-X",
            owner="@tehki",
            reason="r",
            scope="s",
            risk="LOW",
            approved_by="a",
            compensating_controls=(),
            issued_at=now,
            expires_at=now + timedelta(days=1),
            removal_condition="done",
            max_retention_seconds=60.0,
        )

    # A naive timestamp makes expiry depend on the reader's timezone.
    with pytest.raises(RetentionConfigurationError, match="timezone-aware"):
        RetentionOverride(
            record_id="EXC-X",
            owner="@tehki",
            reason="r",
            scope="s",
            risk="LOW",
            approved_by="a",
            compensating_controls=("c",),
            issued_at=datetime(2026, 9, 1),  # naive on purpose
            expires_at=datetime(2026, 9, 2),
            removal_condition="done",
            max_retention_seconds=60.0,
        )

    with pytest.raises(ValueError, match="project_id"):
        EphemeralStore("  ")


def test_cleanup_after_restart() -> None:
    """Startup purges transient locations, because the index of what to delete is gone."""
    clock = ManualClock()
    deleter = FakeDeleter("spill_directory")
    store = make_store(clock, deleters=[deleter])

    store.cleanup_after_restart()

    assert deleter.purge_calls == 1, "a restart must clear leftover transient content"


def test_deletion_failure_behavior() -> None:
    """A failed deletion is reported, retried a bounded number of times, then surfaced.

    It is never reported as a success, and the retry does not become an unbounded loop
    that quietly retains content forever.
    """
    clock = ManualClock()
    sink = RecordingEventSink()
    deleter = FakeDeleter("spill_directory", fail_times=99)
    store = make_store(clock, deleters=[deleter], event_sink=sink, max_deletion_attempts=3)
    handle = store.put(SENSITIVE_TEXT, label="captured_audio_frames")

    clock.advance(11.0)

    first = store.reap()
    assert first.deleted == ()
    assert first.pending_retry == (handle.entry_id,)
    assert not first.ok, "a reap with failures is not a successful reap"
    assert store.state_of(handle) is EntryState.DELETION_PENDING

    store.reap()
    final = store.reap()

    assert final.failed == (handle.entry_id,)
    assert not final.ok
    assert store.state_of(handle) is EntryState.DELETION_FAILED

    # Bounded: no further attempts after the limit.
    attempts_at_limit = deleter.attempts
    store.reap()
    assert deleter.attempts == attempts_at_limit, "retries must be bounded"

    # Every attempt was reported, and the last one said it was giving up.
    assert len(sink.events) == 3
    assert [event.attempts for event in sink.events] == [1, 2, 3]
    assert sink.events[-1].final is True
    assert all(event.location == "spill_directory" for event in sink.events)

    # A transient failure that later succeeds resolves cleanly.
    flaky = FakeDeleter("cache", fail_times=1)
    store2 = make_store(clock, deleters=[flaky])
    handle2 = store2.put(SENSITIVE_TEXT, label="translation_output")
    clock.advance(11.0)
    assert store2.reap().pending_retry == (handle2.entry_id,)
    assert store2.reap().deleted == (handle2.entry_id,)
    assert store2.state_of(handle2) is None


def test_process_memory_is_purged_even_when_another_location_fails() -> None:
    """Process memory is cleared on expiry regardless of what else fails.

    `deletion_locations` in the policy lists `process_memory` first. It matters most in
    the failure path: when an external location cannot be cleaned, the entry stays in the
    index awaiting retry, and without this the content would sit in memory for as long as
    the failure persists — reachable from any traceback or heap dump.

    Added after a mutation test showed the suite passed with the memory purge removed.
    """
    clock = ManualClock()
    deleter = FakeDeleter("spill_directory", fail_times=99)
    store = make_store(clock, deleters=[deleter], max_deletion_attempts=2)
    handle = store.put(SENSITIVE_TEXT, label="speech_recognition_transcript")

    clock.advance(11.0)
    store.reap()

    # The entry is deliberately still tracked so the failure stays visible.
    assert store.state_of(handle) is EntryState.DELETION_PENDING
    retained = store._entries[handle.entry_id]
    assert retained.content != SENSITIVE_TEXT
    assert len(retained.content) == 0, "content must not linger in memory awaiting retry"

    store.reap()
    assert store.state_of(handle) is EntryState.DELETION_FAILED
    assert len(store._entries[handle.entry_id].content) == 0


def test_sensitive_content_redaction() -> None:
    """No repr, str, event or exception carries the content itself."""
    clock = ManualClock()
    sink = RecordingEventSink()
    deleter = FakeDeleter("spill_directory", fail_times=99)
    store = make_store(clock, deleters=[deleter], event_sink=sink)
    handle = store.put(SENSITIVE_TEXT, label="speech_recognition_transcript")

    assert SENSITIVE_TEXT not in repr(store)
    assert SENSITIVE_TEXT not in repr(handle)
    assert SENSITIVE_TEXT not in str(handle)

    # The internal entry is the dangerous one: a default dataclass repr would print the
    # content into any traceback that touched it.
    internal = repr(store._entries[handle.entry_id])
    assert SENSITIVE_TEXT not in internal
    assert "speech_recognition_transcript" in internal

    clock.advance(11.0)
    store.reap()

    event = sink.events[-1]
    assert SENSITIVE_TEXT not in str(event)
    assert SENSITIVE_TEXT not in repr(event)
    assert event.label == "speech_recognition_transcript"

    # And the expiry error names the entry, not the content.
    with pytest.raises(ContentExpiredError) as caught:
        with store.borrow(handle):
            pass
    assert SENSITIVE_TEXT not in str(caught.value)


def test_project_or_tenant_isolation() -> None:
    """A handle from one project is refused by another project's store."""
    clock = ManualClock()
    ours = make_store(clock)
    theirs = EphemeralStore("some-other-project", clock=clock)

    foreign = theirs.put(SENSITIVE_TEXT, label="translation_output")

    with pytest.raises(ProjectIsolationError, match="cross-project"):
        with ours.borrow(foreign):
            pass

    with pytest.raises(ProjectIsolationError):
        ours.is_present(foreign)

    # Forging a handle with the right shape does not help; the project is checked, and a
    # matching project with an unknown id still yields nothing.
    forged = TransientHandle(
        entry_id=foreign.entry_id, project_id="on-the-fly", label="translation_output"
    )
    assert not ours.is_present(forged)
    assert theirs.is_present(foreign), "the owning store is unaffected"


# ======================================================================================
# Drift protection and the rest.
# ======================================================================================


def load_policy() -> dict[str, Any]:
    with POLICY_FILE.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict)
    return data


def test_every_policy_required_test_exists_here() -> None:
    """The policy's required_tests list is enforced, not merely aspirational.

    Adding a requirement to CODING_AGENT_POLICY_v1.3-otf1.yaml fails the build until a test
    with the matching name exists. Without this, the list is a promise nothing keeps.
    """
    required = load_policy()["retention"]["required_tests"]
    defined = set(globals())

    missing = [name for name in required if f"test_{name}" not in defined]
    assert not missing, "retention.required_tests names tests that do not exist: " + ", ".join(
        f"test_{name}" for name in missing
    )


def test_constants_match_the_policy_document() -> None:
    """The runtime default and the policy file cannot drift apart silently."""
    retention = load_policy()["retention"]
    assert retention["default_post_use_seconds"] == DEFAULT_TRANSIENT_RETENTION_SECONDS
    assert (
        retention["classes"]["EPHEMERAL"]["default_post_use_seconds"]
        == DEFAULT_TRANSIENT_RETENTION_SECONDS
    )


def test_store_capacity_is_bounded() -> None:
    """A full store refuses rather than evicting content that may still be in use."""
    clock = ManualClock()
    store = make_store(clock, max_entries=2)
    store.put(b"one", label="captured_audio_frames")
    store.put(b"two", label="captured_audio_frames")

    with pytest.raises(StoreCapacityExceededError, match="backpressure"):
        store.put(b"three", label="captured_audio_frames")


def test_purge_all_ignores_leases_at_shutdown() -> None:
    """Shutdown clears everything; a leaked lease must not keep content alive."""
    clock = ManualClock()
    store = make_store(clock)
    handle = store.put(SENSITIVE_TEXT, label="translation_output")

    with store.borrow(handle):
        report = store.purge_all()

    assert report.deleted == (handle.entry_id,)
    assert len(store) == 0


def test_next_deadline_ignores_leased_entries() -> None:
    clock = ManualClock()
    store = make_store(clock)
    assert store.next_deadline() is None

    handle = store.put(SENSITIVE_TEXT, label="translation_output")
    assert store.next_deadline() == pytest.approx(10.0)

    with store.borrow(handle):
        assert store.next_deadline() is None


def test_threaded_reaper_deletes_without_being_asked() -> None:
    """The production driver actually fires.

    The only test here that uses real time, because the thing under test is whether a
    background thread wakes up on its own. Bounded by an explicit timeout so a failure
    reports rather than hangs.
    """
    store = EphemeralStore("on-the-fly", policy=TransientRetentionPolicy(seconds=0.05))
    handle = store.put(SENSITIVE_TEXT, label="captured_audio_frames")

    with ThreadedReaper(store):
        deadline = time.monotonic() + 5.0
        while store.is_present(handle) and time.monotonic() < deadline:
            time.sleep(0.01)

    assert not store.is_present(handle), "the reaper did not delete on its own"
    assert len(store) == 0


# ---------------------------------------------------------------------------------------
# The reaper, beyond "it fires"
#
# Article 6 requires retention enforcement to be automatic. `EphemeralStore.reap()` does the
# work but has to be called; `ThreadedReaper` is what calls it. Its module docstring says it
# "is tested for the narrower question of whether it wakes up and calls through" — and that
# was one test. Fourteen of its sixteen mutation sites survived the suite, including the
# final reap on shutdown, the purge-on-stop mode, and every branch of the sleep computation.
#
# Most of what it decides needs no thread at all. `_sleep_seconds` is a pure function of the
# store's next deadline and the clock, and it is the one that must never return a negative
# number (a busy spin) or an unbounded one (a missed deletion).
# ---------------------------------------------------------------------------------------


def idle_reaper(store: EphemeralStore, clock: ManualClock, **kwargs: Any) -> ThreadedReaper:
    """A reaper that is never started, for the decisions that do not need a thread."""
    return ThreadedReaper(store, clock=clock, **kwargs)


def test_an_idle_store_still_wakes_periodically() -> None:
    """With nothing to delete the reaper must not sleep forever.

    A deadline added by another thread between a reap and a sleep would otherwise wait on a
    notify that may have been lost.
    """
    clock = ManualClock()
    reaper = idle_reaper(make_store(clock), clock)

    assert reaper._sleep_seconds() == IDLE_WAKE_SECONDS


def test_a_deadline_already_past_is_not_waited_on() -> None:
    """Never negative. A negative timeout on a condition variable returns immediately, so
    the harm is not a hang — it is a loop that spins without bound."""
    clock = ManualClock()
    store = make_store(clock, policy=TransientRetentionPolicy(seconds=10.0))
    store.put(SENSITIVE_TEXT, label="captured_audio_frames")
    clock.advance(30.0)

    assert idle_reaper(store, clock)._sleep_seconds() == 0.0


def test_a_deadline_within_the_idle_window_is_waited_on_exactly() -> None:
    clock = ManualClock()
    store = make_store(clock, policy=TransientRetentionPolicy(seconds=0.25))
    store.put(SENSITIVE_TEXT, label="captured_audio_frames")

    assert idle_reaper(store, clock)._sleep_seconds() == pytest.approx(0.25)


def test_a_deadline_beyond_the_idle_window_is_capped() -> None:
    """Never unbounded. The loop re-reads the next deadline on every wake, so capping the
    sleep is what makes a lost notify cost a delay rather than a missed deletion."""
    clock = ManualClock()
    store = make_store(clock, policy=TransientRetentionPolicy(seconds=10.0))
    store.put(SENSITIVE_TEXT, label="captured_audio_frames")

    assert idle_reaper(store, clock)._sleep_seconds() == IDLE_WAKE_SECONDS


def test_content_held_under_lease_is_treated_as_idle() -> None:
    """A borrowed entry has no deadline until the lease ends, and the reaper must not
    conclude from that that something is overdue."""
    clock = ManualClock()
    store = make_store(clock, policy=TransientRetentionPolicy(seconds=0.25))
    handle = store.put(SENSITIVE_TEXT, label="captured_audio_frames")
    reaper = idle_reaper(store, clock)

    with store.borrow(handle):
        assert store.next_deadline() is None
        assert reaper._sleep_seconds() == IDLE_WAKE_SECONDS


# --- shutdown ---------------------------------------------------------------------------


def test_stopping_reaps_what_was_already_due() -> None:
    """Handbook 35. Shutdown must not strand content that is already past its deadline.

    Asserted without starting the thread, so what is under test is `stop()` itself rather
    than a race with the loop.
    """
    clock = ManualClock()
    store = make_store(clock, policy=TransientRetentionPolicy(seconds=10.0))
    handle = store.put(SENSITIVE_TEXT, label="translation_output")
    clock.advance(30.0)
    reaper = idle_reaper(store, clock)

    assert store.is_present(handle), "still there before the stop"
    reaper.stop()

    assert not store.is_present(handle), "stop() left content past its deadline behind"
    assert reaper.last_report is not None
    assert reaper.last_report.ok


def test_stopping_leaves_content_that_is_not_yet_due() -> None:
    """A final reap is a reap, not a purge. Shutting down does not shorten the window."""
    clock = ManualClock()
    store = make_store(clock, policy=TransientRetentionPolicy(seconds=10.0))
    handle = store.put(SENSITIVE_TEXT, label="translation_output")

    idle_reaper(store, clock).stop()

    assert store.is_present(handle)


def test_purge_on_stop_takes_everything_regardless_of_deadline() -> None:
    """The mode for shutting the application down: nothing survives, due or not."""
    clock = ManualClock()
    store = make_store(clock, policy=TransientRetentionPolicy(seconds=10.0))
    handle = store.put(SENSITIVE_TEXT, label="translation_output")

    reaper = idle_reaper(store, clock, purge_on_stop=True)
    reaper.stop()

    assert not store.is_present(handle)
    assert len(store) == 0


def test_purge_on_stop_reaches_the_deleters_too() -> None:
    """Purging its own memory is not enough; every location the policy lists has to go."""
    clock = ManualClock()
    deleter = FakeDeleter()
    store = make_store(clock, deleters=[deleter], policy=TransientRetentionPolicy(seconds=10.0))
    store.put(SENSITIVE_TEXT, label="captured_audio_frames")

    idle_reaper(store, clock, purge_on_stop=True).stop()

    assert deleter.deleted or deleter.purge_calls, "the spill location was never cleared"


def test_the_last_report_is_the_account_of_the_final_reap() -> None:
    """`stop()` is where a deletion failure at shutdown becomes visible."""
    clock = ManualClock()
    store = make_store(
        clock,
        deleters=[FakeDeleter(fail_times=999)],
        policy=TransientRetentionPolicy(seconds=1.0),
        max_deletion_attempts=1,
    )
    store.put(SENSITIVE_TEXT, label="captured_audio_frames")
    clock.advance(30.0)

    reaper = idle_reaper(store, clock)
    reaper.stop()

    assert reaper.last_report is not None
    assert not reaper.last_report.ok, "a shutdown that could not delete is not clean"


# --- lifecycle --------------------------------------------------------------------------


def test_a_reaper_that_was_never_started_is_not_running() -> None:
    clock = ManualClock()
    reaper = idle_reaper(make_store(clock), clock)

    assert not reaper.running
    assert reaper.last_report is None


def test_starting_twice_does_not_leave_a_second_thread_behind() -> None:
    """Two threads reaping one store is not wrong, but it is not what start() promises."""
    store = EphemeralStore("on-the-fly", policy=TransientRetentionPolicy(seconds=0.05))
    reaper = ThreadedReaper(store)
    reaper.start()
    first = reaper._thread
    try:
        reaper.start()
        assert reaper._thread is first
        assert reaper.running
    finally:
        reaper.stop()

    assert not reaper.running


def test_the_context_manager_starts_and_stops() -> None:
    store = EphemeralStore("on-the-fly", policy=TransientRetentionPolicy(seconds=0.05))
    reaper = ThreadedReaper(store)

    with reaper as entered:
        assert entered is reaper
        assert reaper.running

    assert not reaper.running


def test_notify_is_safe_before_the_thread_exists() -> None:
    """Optional by contract: missing one costs a delay, and calling one early costs
    nothing."""
    clock = ManualClock()
    idle_reaper(make_store(clock), clock).notify()


def test_the_thread_is_named_after_the_project_it_reaps() -> None:
    """One reaper per store, and a thread dump has to say which."""
    store = EphemeralStore("on-the-fly", policy=TransientRetentionPolicy(seconds=0.05))
    reaper = ThreadedReaper(store)
    reaper.start()
    try:
        thread = reaper._thread
        assert thread is not None
        assert "on-the-fly" in thread.name
        assert thread.daemon, "a forgotten stop() must not hang process exit"
    finally:
        reaper.stop()


def test_the_idle_wake_cannot_outlast_the_retention_window() -> None:
    """The bound that makes a lost notify a delay rather than a violation.

    A missed notify costs at most one idle wake. If that wake were longer than the window
    itself, the cost would be content living past its deadline — which is the one thing this
    subsystem exists to prevent, and Article 6 does not have a tolerance for it.
    """
    assert IDLE_WAKE_SECONDS <= DEFAULT_TRANSIENT_RETENTION_SECONDS


def test_stopping_actually_ends_the_thread_rather_than_forgetting_it() -> None:
    """`running` reads the reaper's own handle, which `stop()` clears either way.

    So the handle is not the question. A thread that ignored the stop signal would leave
    `running` False while still alive and still reaping, and only the thread object itself
    can say so.
    """
    store = EphemeralStore("on-the-fly", policy=TransientRetentionPolicy(seconds=0.05))
    reaper = ThreadedReaper(store)
    reaper.start()
    thread = reaper._thread
    assert thread is not None and thread.is_alive()

    reaper.stop()

    assert not thread.is_alive(), "stop() returned while the reaper thread was still running"
    assert not reaper.running


# ---------------------------------------------------------------------------------------
# The boundaries of the Article 13 override
#
# A `RetentionOverride` is what permits EPHEMERAL content to live past ten seconds. Its
# fields, its missing-field refusals and an already-expired record were covered; every
# *boundary* was not, and mutation testing found each one — the comparisons in this file
# could be flipped and the suite stayed green.
#
# Each one is an off-by-one that either grants more retention than was authorised or refuses
# a configuration that was. Article 6 does not have a tolerance for the first.
# ---------------------------------------------------------------------------------------

AT = datetime(2026, 9, 2, tzinfo=UTC)


def override_with(**changes: Any) -> RetentionOverride:
    """The valid override from `active_override`, with individual fields replaced."""
    issued = datetime(2026, 9, 1, tzinfo=UTC)
    fields: dict[str, Any] = {
        "record_id": "EXC-2026-09-01-003",
        "owner": "@tehki",
        "reason": "session transcript review for a specific accessibility trial",
        "scope": "transcripts in the accessibility trial build only",
        "risk": "MODERATE",
        "approved_by": "@tehki",
        "compensating_controls": ("opt-in per session", "local storage only"),
        "issued_at": issued,
        "expires_at": issued + timedelta(days=30),
        "removal_condition": "the trial ends",
        "max_retention_seconds": 3600.0,
    }
    fields.update(changes)
    return RetentionOverride(**fields)


def test_the_reference_override_is_valid_so_the_refusals_below_are_about_one_field() -> None:
    assert override_with().max_retention_seconds == 3600.0


# --- when the authorisation runs ---------------------------------------------------------


def test_an_override_that_expires_the_moment_it_is_issued_is_refused() -> None:
    """Zero-length authorisation. It would satisfy every field check and permit nothing,
    which is a record that reads as an exception without being one."""
    issued = datetime(2026, 9, 1, tzinfo=UTC)

    with pytest.raises(RetentionConfigurationError, match="expires at or before"):
        override_with(issued_at=issued, expires_at=issued)


def test_an_override_is_active_at_the_instant_it_was_issued() -> None:
    """The window is closed at the start: authorisation begins when it says it begins."""
    issued = datetime(2026, 9, 1, tzinfo=UTC)

    assert override_with().is_active(issued)


def test_an_override_is_not_active_at_the_instant_it_expires() -> None:
    """And open at the end. An exception that lasts one moment longer than it says is an
    exception nobody approved for that moment (Article 13)."""
    override = override_with()

    assert not override.is_active(override.expires_at)
    assert override.is_active(override.expires_at - timedelta(microseconds=1))


def test_activity_cannot_be_judged_against_a_naive_datetime() -> None:
    """Otherwise whether the exception is still in force depends on where the reader is."""
    with pytest.raises(RetentionConfigurationError, match="aware datetime"):
        override_with().is_active(datetime(2026, 9, 2))


@pytest.mark.parametrize("field", ["issued_at", "expires_at"])
def test_one_naive_timestamp_is_enough_to_refuse_the_record(field: str) -> None:
    """Both being naive was covered. Either being naive is the actual rule, and one of each
    is the shape a careless edit to an exception record produces.

    This is the case that used to raise `TypeError` instead: Python refuses to compare an
    aware datetime with a naive one, and the ordering check ran first, so the crash came out
    of that comparison and past every caller catching `RetentionConfigurationError`.
    """
    issued = datetime(2026, 9, 1, tzinfo=UTC)
    fields = {"issued_at": issued, "expires_at": issued + timedelta(days=30)}
    fields[field] = fields[field].replace(tzinfo=None)

    with pytest.raises(RetentionConfigurationError, match="timezone-aware"):
        override_with(**fields)


# --- how much it authorises --------------------------------------------------------------


@pytest.mark.parametrize("seconds", [0.0, -1.0])
def test_an_override_permitting_nothing_is_refused(seconds: float) -> None:
    """Zero is the boundary. An override that authorises no retention at all is a record
    that satisfies every field check and grants nothing."""
    with pytest.raises(RetentionConfigurationError, match="positive finite"):
        override_with(max_retention_seconds=seconds)


@pytest.mark.parametrize("seconds", [float("inf"), float("nan")])
def test_an_override_permitting_unbounded_retention_is_refused(seconds: float) -> None:
    """ "Forever" is not a retention window, and Article 13 requires an expiry on the
    authorisation rather than on the content alone."""
    with pytest.raises(RetentionConfigurationError, match="positive finite"):
        override_with(max_retention_seconds=seconds)


def test_a_window_exactly_as_long_as_the_override_permits_is_allowed() -> None:
    """The limit is inclusive. Refusing here would make the number in the record mean one
    less than it says."""
    policy = TransientRetentionPolicy.with_override(3600.0, override_with(), at=AT)

    assert policy.seconds == 3600.0


def test_a_window_one_moment_longer_than_the_override_permits_is_refused() -> None:
    with pytest.raises(RetentionConfigurationError, match="permits at most"):
        TransientRetentionPolicy.with_override(3600.001, override_with(), at=AT)


# --- when an override is needed at all ---------------------------------------------------


def test_the_default_window_itself_needs_no_override() -> None:
    """Exactly ten seconds is the default, not an exception to it. Requiring a record here
    would mean the ordinary case could not be configured without one."""
    TransientRetentionPolicy(seconds=DEFAULT_TRANSIENT_RETENTION_SECONDS).validate(at=AT)


def test_a_window_one_moment_over_the_default_needs_an_override() -> None:
    """And the first moment past it does. This is the line Article 6 draws."""
    with pytest.raises(RetentionConfigurationError, match="no override"):
        TransientRetentionPolicy(seconds=DEFAULT_TRANSIENT_RETENTION_SECONDS + 0.001).validate(
            at=AT
        )


def test_the_smallest_useful_override_is_accepted() -> None:
    """One second is a real authorisation. The refusal is for zero and below, and a bound
    that crept up by one would reject a record somebody had properly approved."""
    assert override_with(max_retention_seconds=1.0).max_retention_seconds == 1.0


def test_an_authorisation_record_cannot_be_edited_after_it_is_validated() -> None:
    """Every check on a `RetentionOverride` happens at construction.

    If the record were mutable, a valid one could be built and its expiry pushed out
    afterwards — which would put the whole of Article 13's validation behind a door that
    does not lock. Immutability is what makes construction the only way in.
    """
    override = override_with()

    with pytest.raises(dataclasses.FrozenInstanceError):
        override.expires_at = override.expires_at + timedelta(days=365)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        override.max_retention_seconds = 1e9  # type: ignore[misc]


def test_a_validated_window_cannot_be_widened_afterwards() -> None:
    """The same argument for the policy: `validate` is checked once, at construction."""
    policy = TransientRetentionPolicy.with_override(3600.0, override_with(), at=AT)

    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.seconds = 99999.0  # type: ignore[misc]
