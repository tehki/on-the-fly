"""The retention window, and the only legitimate way to make it longer.

Two different notions of time appear here, deliberately:

* the retention window itself is measured on a **monotonic** clock, because a deadline
  that can move backwards is a deletion that silently does not happen;
* an exception's validity is measured in **wall-clock dates**, because "expires
  2027-03-01" is a calendar promise made to a human, not an interval.

Mixing them would be a subtle way to lose either property, so they stay separate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from on_the_fly.domain.retention.classes import DEFAULT_TRANSIENT_RETENTION_SECONDS
from on_the_fly.domain.retention.errors import RetentionConfigurationError


@dataclass(frozen=True)
class RetentionOverride:
    """An Article 13 exception permitting content to live longer than the default.

    Every field is required. Article 13 lists exactly these because an exception missing
    any one of them cannot be reviewed, attributed, or removed — and an exception that
    cannot be removed is a permanent policy change wearing a temporary label.

    `expires_at` is not advisory. An expired override authorises nothing, which is
    enforced in `TransientRetentionPolicy`, not left to whoever reads the register.
    """

    record_id: str
    owner: str
    reason: str
    scope: str
    risk: str
    approved_by: str
    compensating_controls: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    removal_condition: str
    max_retention_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "record_id",
            "owner",
            "reason",
            "scope",
            "risk",
            "approved_by",
            "removal_condition",
        ):
            if not str(getattr(self, name)).strip():
                raise RetentionConfigurationError(
                    f"retention override is missing {name!r}; Article 13 requires it"
                )
        if not self.compensating_controls:
            raise RetentionConfigurationError(
                "retention override declares no compensating controls; an exception with "
                "nothing holding the risk down is not an exception"
            )
        if self.expires_at <= self.issued_at:
            raise RetentionConfigurationError(
                "retention override expires at or before it was issued"
            )
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            # A naive datetime means the expiry depends on the reader's timezone, which is
            # not a property an authorisation record may have.
            raise RetentionConfigurationError(
                "retention override timestamps must be timezone-aware"
            )
        if not math.isfinite(self.max_retention_seconds) or self.max_retention_seconds <= 0:
            raise RetentionConfigurationError(
                "retention override max_retention_seconds must be a positive finite number"
            )

    def is_active(self, at: datetime) -> bool:
        """True while the override still authorises anything."""
        if at.tzinfo is None:
            raise RetentionConfigurationError("expiry must be evaluated against an aware datetime")
        return self.issued_at <= at < self.expires_at


@dataclass(frozen=True)
class TransientRetentionPolicy:
    """A validated post-use retention window for EPHEMERAL content.

    Constructing one with a window longer than the ten-second default requires an active
    override that itself permits that window. The check happens here, at construction,
    so an unauthorised configuration cannot exist long enough to retain anything.
    """

    seconds: float
    override: RetentionOverride | None = None

    @classmethod
    def default(cls) -> TransientRetentionPolicy:
        return cls(seconds=DEFAULT_TRANSIENT_RETENTION_SECONDS)

    @classmethod
    def with_override(
        cls, seconds: float, override: RetentionOverride, *, at: datetime
    ) -> TransientRetentionPolicy:
        """Build a longer-than-default policy, validating the override as of `at`."""
        policy = cls(seconds=seconds, override=override)
        policy.validate(at=at)
        return policy

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds):
            raise RetentionConfigurationError("retention seconds must be a finite number")
        if self.seconds <= 0:
            # Zero would mean "delete before use", which no caller can use correctly, and
            # negative is nonsense. Both are configuration defects, not edge cases.
            raise RetentionConfigurationError(
                f"retention seconds must be positive, got {self.seconds!r}"
            )

    def validate(self, *, at: datetime) -> None:
        """Confirm this window is authorised as of `at`. Raises if it is not."""
        if self.seconds <= DEFAULT_TRANSIENT_RETENTION_SECONDS:
            return

        if self.override is None:
            raise RetentionConfigurationError(
                f"a {self.seconds}s retention window exceeds the "
                f"{DEFAULT_TRANSIENT_RETENTION_SECONDS}s default and has no override. "
                "Article 6 requires an explicit, owned, expiring exception recorded in "
                "docs/EXCEPTIONS.md."
            )
        if not self.override.is_active(at):
            raise RetentionConfigurationError(
                f"retention override {self.override.record_id} is not active at {at.isoformat()}; "
                "an expired exception authorises nothing (Article 13)"
            )
        if self.seconds > self.override.max_retention_seconds:
            raise RetentionConfigurationError(
                f"retention override {self.override.record_id} permits at most "
                f"{self.override.max_retention_seconds}s, but {self.seconds}s was requested"
            )
