# ADR 0010 — Tajik is removed from the supported set

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — narrows what the project claims to do
**Amends:** ADR 0007, which admitted Tajik at a second tier

## Context

ADR 0007 admitted eight languages at two tiers, and was explicit that Tajik was the reason
the tier system existed at all. It stayed in the set on the argument that a language served
badly, and *described* as served badly, is better than a language dropped.

Three subsequent findings have made that argument untenable — not one of them fatal alone,
which is exactly why they are worth listing together.

| Stage | Finding | Recorded in |
| --- | --- | --- |
| Streaming recognition | No streaming model exists. A search of 134 published repositories found none | ADR 0007 |
| Batch recognition | The one licence-clean Tajik model is a transformers checkpoint that faster-whisper cannot load without `transformers` and `torch` — the multi-gigabyte dependency ADR 0005 avoided | ADR 0007 |
| Translation | Tajik is absent from M2M-100's 100 languages. `opus-mt-en-tg` could not be confirmed to exist. MADLAD-400 might cover it, is Apache-2.0, and does not fit in memory at 11.8 GB | ADR 0009 |

So the pipeline for Tajik would be: an unverified transcript, from a model nobody has tested
on Tajik speech, fed into a translation stage that does not exist and has no licence-clean
candidate.

## Decision

**Remove Tajik from `SUPPORTED`.** Asking for it is refused the way any unsupported language
is refused, with the same error that names what is supported.

The word being defended is "supported". ADR 0007 was right that a tier label is better than
a silent downgrade — but a tier label describes *degraded* service, and what is on offer
here is not degraded service. It is three unverified stages in a row.

There is also a verification problem that no amount of labelling fixes: **nobody on this
project reads Tajik.** A wrong transcript in a language the maintainer cannot evaluate does
not look wrong. It looks like a translation. For a tool whose entire purpose is to be
believed by someone who cannot check it, shipping a stage nobody can validate is worse than
shipping nothing — the failure is silent and it is confident.

## What is deliberately kept

`RecognitionTier.BATCH` and `batch_languages()` stay, with no members.

Deleting them would be tidier and wrong. The distinction they draw is ADR 0007's central
argument and it survives Tajik's removal intact; the CLI's refusal to stream a
non-streaming language is a guard that should exist *before* the language needing it
arrives, not be reconstructed afterwards. `tests/test_languages.py` asserts the machinery on
a constructed language so it cannot rot while unused.

## Consequences

- The project claims seven languages instead of eight, and every one of them has a published
  streaming model. The set is now uniform, which it has never been before.
- **The tagline is now overstated in a way it previously was not.** "Speak without bounds
  with anyone worldwide" survived ADR 0007 because Tajik was in the set as evidence of reach.
  Removing it makes the gap between the promise and the product wider. That is a copy problem
  to solve honestly, not a reason to keep a language nobody can validate.
- Six of the seven remaining languages are still unmeasured and, per ADR 0008, six are not
  licence-cleared. Removing Tajik does not make them ready.
- Nothing about the retention, capability or governance posture changes.

## What would bring Tajik back

Any one of these, and the removal is reversed rather than argued about again:

1. A published streaming model for Tajik under a licence this project can use.
2. A licence-clean batch model in a format loadable without `torch` — or the publisher route
   in ADR 0009, applied to the checkpoint ADR 0007 found.
3. A licence-clean translation path, which today means a model covering Tajik that fits in
   the memory budget.

Plus, in every case, **someone who can read Tajik well enough to say whether the output is
any good.** The technical blockers are the visible ones; this is the one that decides whether
the result can be trusted.

## Review trigger

When any of the three conditions above is met. Also if the language set is ever expanded —
this ADR is the argument that a language enters the set on evidence, not on ambition, and it
should be re-read before the next one is added.
