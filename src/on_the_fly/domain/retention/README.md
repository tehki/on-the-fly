# Retention enforcement

Runtime enforcement of Constitution Article 6: transient project content lives at most ten
seconds past its last use, and deleting it is automatic rather than something a caller has
to remember.

Start with `__init__.py` for the public API and the wiring example. `docs/RETENTION_POLICY.md`
explains how the retention classes map onto this application and why scrollback is an
exception rather than a feature.

| Module | Responsibility |
| --- | --- |
| `classes.py` | The four retention classes, the ten-second constant, and the entry lifecycle |
| `clock.py` | `Clock` as an injected dependency, so deadlines are tested rather than slept through |
| `policy.py` | The validated window, and `RetentionOverride` — the only way to make it longer |
| `store.py` | `EphemeralStore`: holds content, leases it for use, deletes it on time |
| `reaper.py` | `ThreadedReaper`: makes expiry happen without anyone asking |
| `errors.py` | Failure modes as distinct outcomes, none of which carry content |

This directory is a security-sensitive path in `REPOSITORY_GOVERNANCE_v1.1.yaml`. Changes
here need the retention test suite to pass, and `tests/test_retention.py` is checked
against `retention.required_tests` in the policy file, so a new requirement there fails the
build until it is actually tested.

Two rules worth keeping in mind when extending this:

- **Nothing here may write content to a log, an exception message, or a repr.** `_Entry`
  defines its own `__repr__` for exactly this reason.
- **A failed deletion is never reported as a success.** It has its own terminal state and
  its own security event. Article 6 makes deletion failure a privacy event in its own right.
