"""Runtime enforcement of the project's retention policy.

The rule this package exists to make true, from Constitution Article 6:

> Transient project content defaults to a maximum ten-second post-use retention. The clock
> begins when the content is no longer required for active use.

For a live translator that covers the primary data path — captured audio, recognition
transcripts, translated text, synthesised speech. See `docs/RETENTION_POLICY.md` for how
the classes map onto this application, and `docs/adr/0001-on-device-inference.md` for why
that rule is enforceable at all.

Typical wiring:

```python
store = EphemeralStore("on-the-fly", deleters=[spill_directory])
store.cleanup_after_restart()

with ThreadedReaper(store):
    handle = store.put(audio_frame, label="captured_audio_frames")
    with store.borrow(handle) as frame:
        transcript = recognise(frame)
    # the window on `handle` restarts here and runs out ten seconds later,
    # whether or not anyone touches it again
```

Nothing in this package writes content to a log, an exception message, or a repr.
"""

from __future__ import annotations

from on_the_fly.domain.retention.classes import (
    DEFAULT_TRANSIENT_RETENTION_SECONDS,
    OPERATIONAL_METADATA_RETENTION_DAYS,
    EntryState,
    RetentionClass,
)
from on_the_fly.domain.retention.clock import Clock, ManualClock, SystemClock
from on_the_fly.domain.retention.errors import (
    ContentExpiredError,
    DeletionFailedError,
    ProjectIsolationError,
    RetentionConfigurationError,
    RetentionError,
    StoreCapacityExceededError,
)
from on_the_fly.domain.retention.policy import RetentionOverride, TransientRetentionPolicy
from on_the_fly.domain.retention.reaper import ThreadedReaper
from on_the_fly.domain.retention.store import (
    DEFAULT_MAX_DELETION_ATTEMPTS,
    DEFAULT_MAX_ENTRIES,
    Deleter,
    DeletionFailureEvent,
    EphemeralStore,
    ReapReport,
    RecordingEventSink,
    SecurityEventSink,
    TransientHandle,
)

__all__ = [
    "DEFAULT_MAX_DELETION_ATTEMPTS",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TRANSIENT_RETENTION_SECONDS",
    "OPERATIONAL_METADATA_RETENTION_DAYS",
    "Clock",
    "ContentExpiredError",
    "Deleter",
    "DeletionFailedError",
    "DeletionFailureEvent",
    "EntryState",
    "EphemeralStore",
    "ManualClock",
    "ProjectIsolationError",
    "ReapReport",
    "RecordingEventSink",
    "RetentionClass",
    "RetentionConfigurationError",
    "RetentionError",
    "RetentionOverride",
    "SecurityEventSink",
    "StoreCapacityExceededError",
    "SystemClock",
    "ThreadedReaper",
    "TransientHandle",
    "TransientRetentionPolicy",
]
