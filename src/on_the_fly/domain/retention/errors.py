"""Retention failures, modelled as distinct outcomes rather than one generic error.

Each of these represents a different thing having gone wrong, with a different correct
response (handbook 16). None of them carries content: an exception message is a diagnostic
that travels into logs, crash reports and issue trackers, which is exactly where project
content must never end up (Article 8, invariant 2).
"""

from __future__ import annotations


class RetentionError(Exception):
    """Base class, so callers can catch the whole family at a boundary if they need to."""


class RetentionConfigurationError(RetentionError):
    """A retention configuration was rejected before anything could rely on it.

    Raised at construction rather than at first use. A store configured to keep content
    for an hour with no authorisation is a defect, and it should fail while someone is
    looking at it, not silently retain for an hour in production (handbook 9).
    """


class ProjectIsolationError(RetentionError):
    """A handle from one project was presented to another project's store.

    Article 7 makes project boundaries explicit. Reading across them is not an
    inconvenience to be worked around; it is the boundary doing its job.
    """


class ContentExpiredError(RetentionError):
    """The content was requested after its retention deadline had passed.

    This is a normal, expected outcome rather than a bug: it means the retention rule
    worked. Callers that might race an expiry should handle it explicitly.
    """


class StoreCapacityExceededError(RetentionError):
    """The store is full and refused to accept more content.

    Refusing is the safe failure. Evicting the oldest entry to make room would delete
    content that is still inside its retention window and may still be in use, and
    growing without limit turns noisy input into memory exhaustion (handbook 8). The
    correct response is backpressure upstream, not a bigger store.
    """


class DeletionFailedError(RetentionError):
    """Deletion did not complete in every location it had to.

    Raised only by callers that opt into strict deletion. The store itself reports
    failures through the security event sink and its reap report rather than raising,
    because one location failing must not prevent the remaining locations from being
    cleaned up.
    """
