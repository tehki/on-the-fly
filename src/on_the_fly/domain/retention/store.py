"""An in-memory store whose contents delete themselves.

This is the runtime half of `docs/RETENTION_POLICY.md`. Constitution Article 6 gives
transient project content a ten-second post-use lifetime and requires that enforcement be
automatic — not a cleanup call someone has to remember to make.

Three design decisions carry most of the weight:

**Deletion does not wait to be asked.** A store that expires content when someone next
reads it would keep everything forever in the case that matters most: nobody looks again.
`reap()` is driven by a clock, not by access, and `ThreadedReaper` drives it in production.

**Active use is explicit, not inferred.** Content is read through `borrow()`, which holds
a lease. While a lease is open the content cannot be deleted underneath its user, and when
the lease closes the post-use window starts from that moment. Inferring "in use" from the
last read would delete a buffer halfway through a long transcription.

**A failed deletion is not a deletion.** It gets its own terminal state, its own security
event, and a bounded number of retries. It is never folded into the success path, because
Article 6 makes deletion failure a security and privacy event in its own right.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

from on_the_fly.domain.retention.classes import EntryState, RetentionClass
from on_the_fly.domain.retention.clock import Clock, SystemClock
from on_the_fly.domain.retention.errors import (
    ContentExpiredError,
    ProjectIsolationError,
    StoreCapacityExceededError,
)
from on_the_fly.domain.retention.policy import TransientRetentionPolicy

# A store that can grow without limit turns any noisy input into memory exhaustion
# (handbook 8). The default is generous for an audio pipeline and still finite.
DEFAULT_MAX_ENTRIES = 4096

# Retrying a failing deletion forever is not persistence, it is an unbounded background
# loop that never reports the problem (handbook 37).
DEFAULT_MAX_DELETION_ATTEMPTS = 3


class Deleter(Protocol):
    """A location that may hold a copy of transient content and can remove it.

    The store always purges its own memory. A `Deleter` covers every other location the
    policy lists — temporary directories, caches, spill files, generated exports.
    """

    @property
    def location(self) -> str:
        """A short, non-sensitive name used in security events. Never a path with content."""
        ...

    def delete(self, entry_id: str) -> None:
        """Remove the copy of `entry_id`. Raise if it could not be removed."""
        ...

    def purge_all(self) -> None:
        """Remove everything transient in this location.

        Called at startup. After a restart the store's index of what was written is gone,
        so per-id deletion is impossible and a blanket purge is the only correct action.
        """
        ...


@dataclass(frozen=True)
class DeletionFailureEvent:
    """A security event: content outlived its deadline because deletion failed.

    Deliberately metadata only. This event travels into logs and alerting, so it carries
    what is needed to investigate — which entry, which location, how many attempts, the
    error type — and nothing that would put the content itself there (Article 14).
    """

    entry_id: str
    project_id: str
    label: str
    location: str
    attempts: int
    error_type: str
    final: bool

    def __str__(self) -> str:
        outcome = "giving up" if self.final else "will retry"
        return (
            f"deletion failed: entry={self.entry_id} project={self.project_id} "
            f"label={self.label} location={self.location} attempts={self.attempts} "
            f"error={self.error_type} ({outcome})"
        )


class SecurityEventSink(Protocol):
    """Where deletion failures go. Wiring this to nothing is a policy violation."""

    def deletion_failed(self, event: DeletionFailureEvent) -> None: ...


class RecordingEventSink:
    """A bounded in-memory sink, for tests and for a default that never silently drops.

    Bounded because an unbounded event list is the same memory-growth bug the store itself
    is careful to avoid.
    """

    __slots__ = ("_events", "_max_events")

    def __init__(self, max_events: int = 256) -> None:
        self._events: list[DeletionFailureEvent] = []
        self._max_events = max_events

    def deletion_failed(self, event: DeletionFailureEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max_events:
            del self._events[0]

    @property
    def events(self) -> Sequence[DeletionFailureEvent]:
        return tuple(self._events)


@dataclass(frozen=True)
class TransientHandle:
    """A reference to stored content. Carries no content itself.

    Handles are safe to log, put in a queue, or attach to a correlation record. That is
    the point: the rest of the application passes these around instead of the content.
    """

    entry_id: str
    project_id: str
    label: str

    def __str__(self) -> str:
        return f"{self.project_id}/{self.label}/{self.entry_id}"


@dataclass
class _Entry:
    """Internal state. Never returned to callers, never rendered with its content."""

    content: bytes | str
    label: str
    deadline: float
    state: EntryState = EntryState.ACTIVE
    leases: int = 0
    deletion_attempts: int = 0
    failed_locations: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        # Redaction is enforced here rather than left to callers. A dataclass repr would
        # print the content into any traceback that touches this object.
        return (
            f"_Entry(label={self.label!r}, state={self.state}, leases={self.leases}, "
            f"bytes={len(self.content)}, deadline={self.deadline:.3f})"
        )


@dataclass(frozen=True)
class ReapReport:
    """What a single reap actually did. The store's honest account of itself."""

    deleted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    pending_retry: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True only when nothing failed. A reap with failures is not a successful reap."""
        return not self.failed and not self.pending_retry


class EphemeralStore:
    """Holds EPHEMERAL content for one project and deletes it on time.

    Scoped to a single project. Article 7 makes project boundaries explicit, so a handle
    issued by one store is rejected by another rather than quietly resolved.
    """

    def __init__(
        self,
        project_id: str,
        *,
        clock: Clock | None = None,
        policy: TransientRetentionPolicy | None = None,
        deleters: Sequence[Deleter] = (),
        event_sink: SecurityEventSink | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_deletion_attempts: int = DEFAULT_MAX_DELETION_ATTEMPTS,
    ) -> None:
        if not project_id.strip():
            raise ValueError("project_id is required; unscoped stores cannot be isolated")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_deletion_attempts <= 0:
            raise ValueError("max_deletion_attempts must be positive")

        self._project_id = project_id
        self._clock = clock if clock is not None else SystemClock()
        self._policy = policy if policy is not None else TransientRetentionPolicy.default()
        self._deleters = tuple(deleters)
        self._events = event_sink if event_sink is not None else RecordingEventSink()
        self._max_entries = max_entries
        self._max_deletion_attempts = max_deletion_attempts

        # A reaper thread calls reap() while the pipeline calls put() and borrow(), so
        # every mutation of _entries is guarded. RLock because deletion runs under the
        # same lock that discovered the expiry.
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}

    # -- properties ------------------------------------------------------------------

    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def retention_class(self) -> RetentionClass:
        return RetentionClass.EPHEMERAL

    @property
    def retention_seconds(self) -> float:
        return self._policy.seconds

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        return (
            f"EphemeralStore(project_id={self._project_id!r}, "
            f"retention_seconds={self._policy.seconds}, entries={len(self)})"
        )

    # -- storing and reading ----------------------------------------------------------

    def put(self, content: bytes | str, *, label: str) -> TransientHandle:
        """Store content and start its post-use clock.

        `label` names the kind of data, not the data — "captured_audio_frames", not a
        transcript. It appears in security events, so it must stay non-sensitive.
        """
        if not label.strip():
            raise ValueError("label is required; unlabelled content cannot be classified")

        entry_id = uuid.uuid4().hex
        with self._lock:
            if len(self._entries) >= self._max_entries:
                # Refusing is the safe failure. Evicting the oldest would delete content
                # that is still within its window and might still be in use.
                raise StoreCapacityExceededError(
                    f"store for project {self._project_id!r} is at capacity "
                    f"({self._max_entries} entries); apply backpressure upstream"
                )
            self._entries[entry_id] = _Entry(
                content=content,
                label=label,
                deadline=self._clock.now() + self._policy.seconds,
            )
        return TransientHandle(entry_id=entry_id, project_id=self._project_id, label=label)

    @contextmanager
    def borrow(self, handle: TransientHandle) -> Iterator[bytes | str]:
        """Use the content, holding it against deletion for the duration.

        On exit the post-use window restarts from now. That is what "continued legitimate
        use MAY refresh the window" means in practice: an hour-long conversation never
        accumulates an hour of retained audio, because each buffer's window restarts and
        then runs out once that buffer is genuinely done with.
        """
        self._check_project(handle)
        with self._lock:
            entry = self._entries.get(handle.entry_id)
            if entry is None or entry.state is not EntryState.ACTIVE:
                raise ContentExpiredError(
                    f"content {handle.entry_id} is no longer available; its retention "
                    "window has passed"
                )
            entry.leases += 1
            content = entry.content
        try:
            yield content
        finally:
            with self._lock:
                current = self._entries.get(handle.entry_id)
                if current is not None:
                    current.leases -= 1
                    # Post-use clock restarts at the end of use, not at the last read.
                    current.deadline = self._clock.now() + self._policy.seconds

    def is_present(self, handle: TransientHandle) -> bool:
        """True while the content is still stored and usable."""
        self._check_project(handle)
        with self._lock:
            entry = self._entries.get(handle.entry_id)
            return entry is not None and entry.state is EntryState.ACTIVE

    def state_of(self, handle: TransientHandle) -> EntryState | None:
        """The lifecycle state, or None once the entry is fully gone."""
        self._check_project(handle)
        with self._lock:
            entry = self._entries.get(handle.entry_id)
            return entry.state if entry is not None else None

    def _check_project(self, handle: TransientHandle) -> None:
        if handle.project_id != self._project_id:
            raise ProjectIsolationError(
                f"handle belongs to project {handle.project_id!r} but this store serves "
                f"{self._project_id!r}; cross-project access is denied by default (Article 7)"
            )

    # -- expiry ------------------------------------------------------------------------

    def next_deadline(self) -> float | None:
        """The earliest deadline among unleased entries, or None if there is nothing to do.

        Used by `ThreadedReaper` to sleep exactly as long as it should rather than polling.
        """
        with self._lock:
            deadlines = [
                entry.deadline
                for entry in self._entries.values()
                if entry.leases == 0 and entry.state is EntryState.ACTIVE
            ]
            pending = any(
                entry.state is EntryState.DELETION_PENDING for entry in self._entries.values()
            )
        if pending:
            # A pending retry should be attempted promptly rather than waiting for some
            # unrelated entry's deadline.
            return self._clock.now()
        return min(deadlines) if deadlines else None

    def reap(self) -> ReapReport:
        """Delete everything past its deadline, and retry anything still pending.

        Safe to call at any time and from any thread. Calling it more often than necessary
        costs a lock acquisition; calling it too rarely is what the reaper exists to prevent.
        """
        now = self._clock.now()
        deleted: list[str] = []
        failed: list[str] = []
        pending: list[str] = []

        with self._lock:
            due = [
                entry_id
                for entry_id, entry in self._entries.items()
                if entry.state is EntryState.DELETION_PENDING
                or (
                    entry.state is EntryState.ACTIVE and entry.leases == 0 and entry.deadline <= now
                )
            ]
            for entry_id in due:
                entry = self._entries[entry_id]
                outcome = self._delete_entry(entry_id, entry)
                if outcome is EntryState.DELETED:
                    deleted.append(entry_id)
                elif outcome is EntryState.DELETION_FAILED:
                    failed.append(entry_id)
                else:
                    pending.append(entry_id)

        return ReapReport(
            deleted=tuple(deleted), failed=tuple(failed), pending_retry=tuple(pending)
        )

    def _delete_entry(self, entry_id: str, entry: _Entry) -> EntryState:
        """Delete one entry from every location. Caller holds the lock."""
        entry.state = EntryState.EXPIRED

        # Process memory goes first and unconditionally. It is the one location fully
        # under this object's control, so even a total failure elsewhere still removes the
        # copy that is easiest to leak through a traceback or a heap dump.
        entry.content = b""

        entry.deletion_attempts += 1
        still_failing: list[str] = []

        for deleter in self._deleters:
            # Deliberately broad: a deleter is external code, and ANY way it can fail is a
            # reportable retention failure. Narrowing this would let an unanticipated
            # error type escape and abort the deletion of the remaining locations.
            try:
                deleter.delete(entry_id)
            except Exception as exc:
                still_failing.append(deleter.location)
                final = entry.deletion_attempts >= self._max_deletion_attempts
                self._events.deletion_failed(
                    DeletionFailureEvent(
                        entry_id=entry_id,
                        project_id=self._project_id,
                        label=entry.label,
                        location=deleter.location,
                        attempts=entry.deletion_attempts,
                        error_type=type(exc).__name__,
                        final=final,
                    )
                )

        entry.failed_locations = still_failing

        if not still_failing:
            del self._entries[entry_id]
            return EntryState.DELETED

        if entry.deletion_attempts >= self._max_deletion_attempts:
            # Terminal, and deliberately still present in the index. An operator needs to
            # be able to see that a location was never cleaned; dropping the record here
            # would turn a known failure into an invisible one.
            entry.state = EntryState.DELETION_FAILED
            return EntryState.DELETION_FAILED

        entry.state = EntryState.DELETION_PENDING
        return EntryState.DELETION_PENDING

    # -- lifecycle ---------------------------------------------------------------------

    def cleanup_after_restart(self) -> None:
        """Purge transient locations at startup (handbook 35).

        In-process content does not survive a restart, but anything spilled to disk or
        cache does — and the index that named it did not. Per-id deletion is therefore
        impossible and a blanket purge of each transient location is the only correct
        action. A location that cannot be purged raises, because starting up while unable
        to clear leftover content is a fail-open we should not perform silently.
        """
        for deleter in self._deleters:
            deleter.purge_all()

    def purge_all(self) -> ReapReport:
        """Delete everything now, regardless of deadline. Used on shutdown.

        Leases are ignored deliberately: at shutdown there is no legitimate continued use
        to protect, and content surviving because a lease leaked is the worse outcome.
        """
        deleted: list[str] = []
        failed: list[str] = []
        pending: list[str] = []
        with self._lock:
            for entry_id in list(self._entries):
                entry = self._entries[entry_id]
                outcome = self._delete_entry(entry_id, entry)
                if outcome is EntryState.DELETED:
                    deleted.append(entry_id)
                elif outcome is EntryState.DELETION_FAILED:
                    failed.append(entry_id)
                else:
                    pending.append(entry_id)
        return ReapReport(
            deleted=tuple(deleted), failed=tuple(failed), pending_retry=tuple(pending)
        )
