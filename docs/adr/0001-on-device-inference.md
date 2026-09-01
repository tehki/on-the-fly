# ADR 0001 — Speech recognition and translation run on the user's device

**Status:** Accepted
**Date:** 2026-09-01
**Deciders:** @tehki
**Risk:** MODERATE — this decision constrains every later component choice

## Context

On-the-fly translates live speech. Two requirements were given: the project must be free
to run long-term, and it must not depend on the paid Google Translate API.

A third requirement is not negotiable and comes from the policy stack rather than from
product preference. Constitution Article 6 gives transient project content a ten-second
post-use lifetime, and Article 8 invariant 10 forbids claiming a deletion, encryption, or
isolation guarantee that has not been verified.

Those two facts interact in a way that settles the architecture.

## Decision

All default speech recognition and translation runs on the user's device. No audio,
transcript, or translation leaves the machine in the default configuration.

A remote engine may later be offered as an explicitly opt-in, off-by-default, per-session
choice. If it is, the user is told plainly that their audio leaves the device and that this
project's retention guarantees end at that boundary. It is never the default and never
silent.

## Why not a cloud API

**Paid APIs** (Google, DeepL, Azure, AWS) fail the cost requirement directly. A live
translator sends continuous audio; per-character and per-minute billing on a continuous
stream is the expensive shape of these services, not the cheap one.

**Free public API instances** (public LibreTranslate deployments, MyMemory, Lingva and
similar) look like they satisfy "free API" but fail on three grounds:

1. *Retention.* Once audio or text reaches a third party, its lifetime is governed by
   their policy. We could not enforce the ten-second window, and we could not truthfully
   claim deletion. Article 8 invariant 10 does not permit stating a guarantee we cannot
   verify — so the honest version of that product would have to tell users their speech is
   sent to an unknown operator and kept for an unknown period.
2. *Longevity.* "Free long-term" is the one thing a third-party free tier cannot promise.
   Public instances get rate-limited, put behind keys, or shut down. A dependency that can
   be withdrawn is not a foundation for a project whose stated requirement is to stay free.
3. *Reliability.* Rate limits and shared-instance latency are poor material for a
   real-time conversational tool.

**On-device inference** satisfies all three: no per-request cost, no rate limit, no vendor
who can change the terms, and a retention guarantee we can actually enforce and therefore
actually state. It also works with no network at all, which is a genuine feature for a
travel translator.

## Consequences

**Gained**

- The ten-second retention rule becomes enforceable rather than aspirational.
- Zero marginal cost per translation, permanently.
- Works offline.
- No API keys, so no secret to leak (Article 8 invariant 2 becomes easy rather than hard).

**Accepted costs**

- Models must be downloaded, cached, and integrity-verified on first run. Model weights are
  dependencies under Article 12: each needs a recorded source, publisher, licence,
  checksum, cache location, retention class, and a fail-closed path when verification
  fails.
- Inference consumes the user's CPU and memory. This makes model size a product decision,
  not just a technical one, and it is why `docs/PERFORMANCE_BUDGET.md` exists.
- Accuracy on a small local model is below a large hosted one. Accepted.
- First-run experience includes a model download of meaningful size.

**Licence constraint worth stating once, because no scanner catches it**

Several of the strongest multilingual models are released under non-commercial terms —
NLLB-200 is CC-BY-NC-4.0, Coqui XTTS is CPML. This repository is Apache-2.0. Those models
are excluded from the default pipeline. `MADLAD-400` is Apache-2.0 and remains available:
"no Google" was clarified to mean no paid API, not the organisation, and model weights are
not an API.

## Candidate components

All permissively licensed, all local, none requiring a paid service. To be confirmed by
measurement, not adopted on this list alone.

| Stage | Candidate | Licence |
| --- | --- | --- |
| Voice activity detection | Silero VAD | MIT |
| Speech recognition | faster-whisper (CTranslate2) | MIT |
| Translation | Opus-MT (Helsinki-NLP) or Argos Translate | MIT / CC-BY |
| Translation, wide coverage | MADLAD-400 | Apache-2.0 |
| Speech output (optional) | Piper | MIT |

Each becomes a dependency-admission decision at the point it is actually added, per
Article 12. Listing a candidate here is not admitting it.

## Review trigger

Revisit if on-device latency cannot meet `docs/PERFORMANCE_BUDGET.md` on target hardware,
or if a translation service appears that is both genuinely free long-term and able to offer
a verifiable retention guarantee.
