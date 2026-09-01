"""Retention classes and the constants that define them.

These mirror Constitution Article 6 and the `retention` block of
`CODING_AGENT_POLICY_v1.2.yaml`. The values are here in one place so that a change to the
policy and a change to the runtime cannot drift apart quietly; `tests/test_retention.py`
asserts these constants against the policy file itself.
"""

from __future__ import annotations

from enum import Enum

# Article 6: transient project content defaults to a maximum ten-second post-use window.
DEFAULT_TRANSIENT_RETENTION_SECONDS = 10.0

# Article 6: metadata-only operational records may live longer, precisely because they
# carry no content. This constant is here for completeness; nothing in this module stores
# operational metadata, and nothing that does may store content under it.
OPERATIONAL_METADATA_RETENTION_DAYS = 30


class RetentionClass(Enum):
    """The four classes every piece of project data must belong to.

    There is deliberately no default and no UNCLASSIFIED member. Article 6 requires
    explicit classification, and an unclassified path should fail to construct rather
    than fall into whichever class happens to be least restrictive.
    """

    EPHEMERAL = "EPHEMERAL"
    OPERATIONAL_METADATA = "OPERATIONAL_METADATA"
    DURABLE_PROJECT_ARTIFACT = "DURABLE_PROJECT_ARTIFACT"
    SECURITY_INCIDENT_HOLD = "SECURITY_INCIDENT_HOLD"

    def __str__(self) -> str:
        return self.value


class EntryState(Enum):
    """The lifecycle of a stored item, made explicit rather than implied (handbook 43).

    ```text
    ACTIVE -> EXPIRED -> DELETION_PENDING -> DELETED
                                          -> DELETION_FAILED
    ```

    `DELETION_FAILED` is a distinct terminal state on purpose. Collapsing it into
    `DELETED` would let a failed deletion be reported as a success, which Article 6
    classifies as a security and privacy event in its own right.
    """

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    DELETION_PENDING = "DELETION_PENDING"
    DELETED = "DELETED"
    DELETION_FAILED = "DELETION_FAILED"

    def __str__(self) -> str:
        return self.value
