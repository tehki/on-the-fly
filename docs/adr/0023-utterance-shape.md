# ADR 0023 — An instrument that could not take its own reading

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** LOW — reporting only; no change to what is recognised or translated

## Context

ADR 0022 fixed an utterance ceiling that could never fire, and closed by saying the fix had
not been tested against live speech. A run was made to test it. It produced two finals,
starting at 0.00 s and 27.04 s, and **the output could not say whether the ceiling had
fired.**

The reason is in the recogniser. An endpoint that decodes no text — a pause, a silent room —
resets the utterance without emitting anything, so from outside, these are identical:

- the ceiling firing repeatedly through a long silence, emitting nothing each time;
- the ceiling never firing at all.

Both show one large gap between two finals. `listen` was extended specifically to make live
endpointing observable, and it could not answer the question it was built for.

## Decision

**Report the shape of each utterance, and count the endpoints that produce no text.**

```text
TranscriptEvent.duration_seconds   how long it ran
TranscriptEvent.end_reason         SILENCE | MAX_DURATION | FLUSH
TranscriptEvent.shape              "8.16s MAX_DURATION", or "" when unknown
SherpaStreamingRecognizer          .silent_endpoints
```

`EndReason` already existed on the batch path, with exactly these three cases, so it is
reused rather than duplicated: silence ended it, the ceiling ended it, or the audio ran out.

**The end reason is inferred, not reported.** sherpa says only *that* an endpoint occurred,
never which of its three rules fired. Rule 3 is a clock and triggers exactly at the ceiling
while the silence rules can only trigger before it, so the length distinguishes them. The
method is named `_why_it_ended` and documented as an inference, because a measurement whose
provenance is a deduction should say so. It is wrong in one case: an utterance ended by
silence at exactly the ceiling is reported as the ceiling. Nothing distinguishes those.

Both fields default to `None`. The `StreamingRecognizer` port does not require them, `shape`
is empty when they are absent, and `listen` omits the endpoint line for a recogniser that
does not count — so a future implementation is free not to know.

## Retention

Durations, reasons and counts. `shape` is built from a duration and an enum and **cannot
contain transcript text**, which a test asserts, because it is the one field here intended to
be printed next to a caption and logged. `docs/RETENTION_POLICY.md` classifies these as
`OPERATIONAL_METADATA`; Article 14 permits logging them and still does not permit logging
what was said.

## What this does not do

- **It does not change endpointing.** Same rules, same values, same cuts as ADR 0022.
- **It does not measure pauses.** It measures utterances. The distribution of silence in real
  speech is what `rule2` would have to be tuned against, and reading it off would mean
  retaining audio, which `docs/RETENTION_POLICY.md` does not allow. What can be had is the
  effect of those pauses — how often `SILENCE` ends an utterance rather than `MAX_DURATION` —
  which is the number the tuning decision actually turns on.
- **It does not make the GUI report any of this.** The window shows a caption, not a
  measurement, and that is right for it.

## Consequences

- `listen` prints `(8.16s MAX_DURATION)` beside each final, and a line reading
  `endpoints  N with text, M with none (silence)`.
- Immediately visible on the two available samples: continuous speech ends three times on
  `MAX_DURATION`, and twelve seconds of appended silence produces **four** silent endpoints
  and no finals. The first of those is `MAX_DURATION` rather than `SILENCE` even though the
  speech stops well before the ceiling — which is a question about `rule2` that this ADR
  raises and does not answer.
- A live run can now distinguish a speaker who pauses from an endpointer that does not fire.

## Review trigger

When a live run is measured with this instrumentation, which is the first evidence that could
justify changing `SILENCE_AFTER_SPEECH_SECONDS`.
