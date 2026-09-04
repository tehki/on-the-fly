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

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from on_the_fly.domain.retention import (
    DEFAULT_TRANSIENT_RETENTION_SECONDS,
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
