# ADR 0014 — Both models run single-threaded

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** LOW — two defaults, both measured, both reversible by an argument

## Context

The ninth measurement bounded CTranslate2 to one thread after finding the default — every
core — was **6.9x slower under load**. That looked like a quirk of one library.

The tenth measurement then put recognition at 0.848x real time with three of four cores
busy: still keeping up, but the thinnest margin anywhere in the pipeline. Past 1.0x the
recogniser falls behind the speaker, and at that point nothing downstream matters, because
translation tuning cannot rescue a transcript that arrives after the next sentence.

That number came from one machine at one core count, with the CLI's default of **two**
recogniser threads. So: how much hardware does keeping up actually require?

## What was measured

`taskset` to constrain available cores, `num_threads` varied, 23 seconds of real speech,
three passes each. Real-time factor, because it has an absolute meaning: below 1.0 the
pipeline keeps pace, above it, it does not.

| `num_threads` | 1 core | 2 cores | 4 cores |
| --- | --- | --- | --- |
| **1** | **0.307** | **0.337** | **0.315** |
| 2 (the shipped default) | 0.779 | 0.740 | 0.470 |
| 4 | 1.604 ✗ | 1.378 ✗ | 1.181 ✗ |

**One thread is fastest at every core count, and four threads fall behind real time at all
of them** — including with four cores available, which is the configuration that ought to
suit it best.

Transcripts are byte-identical across all three settings. This is speed with no quality
trade at all, which is unusual enough to be worth stating plainly.

## Decision

**The recogniser defaults to one thread.** The CLI's `--threads` default changes from 2 to
1. `SherpaStreamingRecognizer` already defaulted to 1; only the command line disagreed.

Together with the ninth measurement's change, **both models in the pipeline now run
single-threaded by default.**

## Why more threads are slower

Streaming recognition consumes 20 ms chunks. Each chunk is a small amount of arithmetic, and
splitting it across threads costs a synchronisation round trip per chunk that is larger than
the work saved. The same reasoning explains the translation result: a short sentence is not
enough work to amortise coordinating four cores.

Threading helps when a unit of work is large. Both halves of this pipeline are deliberately
made of small units — that is what makes it *streaming* — so it is the wrong tool in both
places.

## Consequences

- **Recognition keeps up on a single core**, at 0.307x. The concern the tenth measurement
  raised — that a weaker machine would push it past real time — is answered, and answered
  better than expected: it was the thread count, not the core count.
- The tenth measurement's 0.848x under load was taken at two threads. The same conditions at
  one thread should be materially better, and that has **not** been re-measured.
- Both defaults are still parameters. A caller with cores to spare can raise either.
- The pipeline now leaves cores free rather than claiming them, which for a translator
  running alongside a video call is the behaviour that matters.

## What this does not establish

The samples are 23 seconds of clean read English from the model publisher. The *ordering* is
consistent and the differences are large — two- to five-fold, far beyond any plausible noise
— but these are not percentiles over a corpus, and `taskset` restricting cores is not the
same as owning a slower CPU. A four-core machine pretending to have one still has that
machine's cache and memory bandwidth.

The claim is "more threads are slower for this workload on this hardware", which the data
supports comfortably. It is not "one thread is optimal on all hardware", which would need
hardware this project does not have.

## Review trigger

Before phase 2. Mobile CPUs have different core layouts — efficiency and performance cores
with asymmetric scheduling — and a threading decision made on a symmetric x86 laptop should
be re-measured there rather than inherited.
