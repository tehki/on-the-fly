# Retention policy

This document maps the retention classes in `CODING_AGENT_POLICY_v1.2.yaml` onto the data
this application actually handles. Constitution Article 6 requires every file, log,
message, prompt, response, tool payload, cache, trace, and intermediary artefact to carry
a class. Nothing is unclassified by default.

## Why this shapes the product rather than following it

On-the-fly is a live translator. Its primary data — captured audio, recognition
transcripts, translated text, synthesised speech — is exactly what the policy calls
transient project content. So the ten-second `EPHEMERAL` default is not a logging detail
bolted on at the end; it is the constraint the audio pipeline is designed around.

The practical consequence, stated plainly so it is not discovered late:

> **Live translation is fine. Scrollback is not.**

Continued legitimate use refreshes the post-use window, so a conversation that runs for an
hour never accumulates an hour of retained content — each buffer is released once it is no
longer needed for the translation in flight. But any feature that lets someone look at
what was said earlier is retention beyond the window and needs an explicit exception in
`docs/EXCEPTIONS.md` with an owner and an expiry.

## Classes as they apply here

### `EPHEMERAL` — 10 seconds post-use

The clock starts when the data is no longer required for active use. Enforcement must be
automatic, on a scheduler; it must not depend on a developer remembering to call a
cleanup function.

| Data | Notes |
| --- | --- |
| Captured audio frames | Never written to disk by default. Held in a bounded ring buffer only as long as the recogniser needs them. |
| Recognition transcripts | Released once translated and rendered. |
| Translation output | Released once displayed or spoken. |
| Synthesised speech audio | Released once played. |
| Caption scrollback | Disabled by default (`default_retention_seconds: 0`). Enabling it is an exception. |
| Any log or trace containing the above | Content-bearing logs are `EPHEMERAL`; they never inherit the 30-day metadata window. |
| Temporary files, extracted text, queue payloads | Deleted on the same clock, in every location listed under `deletion_locations` in the policy. |

### `OPERATIONAL_METADATA` — 30 days, metadata only

May record that a translation happened and how it performed. May never record what was
said.

Permitted: timestamp, correlation ID, pseudonymous session identifier, source and target
language codes, audio duration, model identifier, latency, error class, outcome,
retention class, deletion result.

Prohibited: transcript text, translated text, audio, any excerpt or truncation of them,
any hash that could confirm a guessed phrase, and any metric label or log field carrying
content.

### `DURABLE_PROJECT_ARTIFACT`

Source, tests, documentation, the policy stack, reviewed configuration, ADRs, model
metadata. Durable because it was chosen to be, not because it happened to persist.

### `SECURITY_INCIDENT_HOLD`

Narrow, owned, access-controlled, expiring. Requires a record in `docs/EXCEPTIONS.md`.

## Deletion is a security control

A failed deletion is a security and privacy event (Constitution Article 6). It is reported
as a failure. It is never logged as success, never swallowed, and a retry loop must not
become indefinite retention by another name.

Deletion must reach every location the policy lists: primary storage, process memory,
replicas, caches, indexes, queues, temporary directories, local workspaces, generated
exports, and any observability storage holding content.

## Implementation status

Not yet implemented. `src/on_the_fly/domain/retention/` is reserved for this code and is
listed as a security-sensitive path so that it arrives under code ownership from its first
commit.

The policy names the enforcement mechanism as scheduled expiry. When the module is built,
the tests listed under `retention.required_tests` in `CODING_AGENT_POLICY_v1.2.yaml` are
required, not optional — including automatic expiry without a follow-up read, post-use
refresh, no premature deletion during active use, cleanup after restart, and deletion
failure behaviour. They must use a controllable clock rather than real sleeps, so they
stay deterministic (handbook 18).

Until that module exists, this repository holds no runtime retention enforcement, and no
document here should be read as claiming otherwise.
