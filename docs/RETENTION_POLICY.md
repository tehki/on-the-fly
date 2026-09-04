# Retention policy

This document maps the retention classes in `CODING_AGENT_POLICY_v1.3-otf1.yaml` onto the data
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

## Implementation

`src/on_the_fly/domain/retention/` implements this policy. Every test named in
`retention.required_tests` exists in `tests/test_retention.py`, and a meta-test reads that
list from the policy file and fails the build if a required test is missing — so the list
cannot become aspirational.

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

Four design decisions are worth knowing before using it:

**Expiry does not wait to be asked.** `reap()` is driven by a clock, and `ThreadedReaper`
drives it on a background thread. A store that expired content on the next read would keep
everything forever in the case that matters most — content nobody looks at again.

**Active use is explicit.** Content is read through `borrow()`, which holds a lease.
Nothing is deleted under an open lease, and the post-use window restarts when the lease
closes. Inferring "in use" from the last read would delete a buffer halfway through a long
transcription; that case is covered by `test_no_premature_deletion_during_active_use`.

**A failed deletion is not a deletion.** It has its own terminal state
(`DELETION_FAILED`), emits a metadata-only `DeletionFailureEvent`, and retries a bounded
number of times. Process memory is purged first and unconditionally, so a location that
cannot be cleaned does not also leave content in the heap awaiting retry.

**Longer retention needs a real exception.** `RetentionOverride` requires all nine
Article 13 fields, must be timezone-aware, and stops authorising anything past its own
`expires_at` — checked at construction, not left to whoever reads the register.

Tests run on an injected `ManualClock`, not real sleeps, so a ten-second deadline is
asserted at 9.999s and 10.001s rather than waited out (handbook 18).

### What is wired, and what is not

The module is wired in. `app/pipeline.py` constructs an `EphemeralStore` and starts a
`ThreadedReaper` for every run; the segmenter puts captured audio frames through it and the
pipeline puts recognition transcripts through it; and every CLI command ends by stating
whether the run finished holding nothing, returning a distinct exit code when it did not.

Still outstanding:

- **No `Deleter` for a real spill location.** Nothing spills to disk yet, so the store is
  constructed with an empty deleter list and only process memory is purged. The temporary
  directories and model caches that will need one get theirs when those locations exist.
- **`OPERATIONAL_METADATA` has a constant here and no storage behind it.** The counters in
  `domain/audio/session.py` and the microphone adapter are metadata-shaped and in-memory;
  nothing is retained for 30 days because nothing is written down at all.
