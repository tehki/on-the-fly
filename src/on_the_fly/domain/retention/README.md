# Retention enforcement

Reserved for the runtime that enforces `docs/RETENTION_POLICY.md`. **Nothing is
implemented yet.**

This directory is listed as a security-sensitive path in
`REPOSITORY_GOVERNANCE_v1.1.yaml` so that retention code arrives under code ownership
from its first commit rather than being brought under governance afterwards.

What belongs here:

- the retention classes as types, so an unclassified data path does not compile;
- a scheduler that expires transient content ten seconds after last use, without
  requiring a follow-up read to trigger it;
- post-use refresh, so continued legitimate use extends the window rather than
  accumulating retained content;
- deletion that reaches every location listed under `deletion_locations` in
  `CODING_AGENT_POLICY_v1.2.yaml`;
- deletion failure surfaced as a security event, never swallowed and never retried
  indefinitely.

The tests named in `retention.required_tests` in the policy are required for this
module. They use a controllable clock, not real sleeps (handbook 18).
