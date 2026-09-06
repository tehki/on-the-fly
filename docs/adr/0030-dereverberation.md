# ADR 0030 — Dereverberation measured, and not adopted

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** LOW — a measurement and a dependency decision; no code ships

## Context

[ADR 0027](0027-reverberation.md) established reverberation as what breaks live recognition,
[ADR 0028](0028-blind-reverberation-estimate.md) established that it cannot be detected from
anything this project measures, and [ADR 0029](0029-measured-room.md) measured the reference
room at RT60 0.6–0.9 s — the livelier half of the grid. Each of those ends by pointing at the
same remaining option: stop measuring the problem and remove it.

WPE (weighted prediction error) is the standard method. Adopting it is a dependency-admission
decision under Article 12, so it was measured before being proposed rather than after.

Single-channel WPE was implemented in about eighty lines of numpy for the measurement — STFT,
per-bin weighted linear prediction from delayed frames, iterated. Not to ship; to find out
whether shipping is worth arguing for.

## It helps, consistently

48-word sample, synthetic rooms, level held constant:

| RT60 | distance | WER before | WER after |
| --- | --- | --- | --- |
| 0.5 | 1.5 m | 29.2% | **20.8%** |
| 0.7 | 1.0 m | 87.5% | **54.2%** |
| 0.7 | 2.0 m | 75.0% | **60.4%** |
| 0.9 | 1.0 m | 100% | **91.7%** |

## And it does not damage what already works

The question that decides whether it could be applied unconditionally, given ADR 0028 showed
there is no way to switch it on at the right moment:

| input | before | after |
| --- | --- | --- |
| clean, no room | 0.0% | 0.0% |
| RT60 0.7 at 0.4 m | 2.1% | 0.0% |
| RT60 0.5 at 0.7 m | 2.1% | 2.1% |

Never worse. That removes the objection that killed the advice in ADR 0028 — this needs no
detector, because it can simply always be on.

## Tuned, on the worst case

So that the decision is not made against a badly configured version of the method:

| taps | delay | iterations | WER | time for 16.7 s |
| --- | --- | --- | --- | --- |
| — | — | — | 87.5% | — |
| 10 | 3 | 3 | 54.2% | 4.2 s |
| 20 | 3 | 3 | 45.8% | 6.6 s |
| **40** | **3** | **3** | **41.7%** | **14.0 s** |
| 30 | 3 | 3 | 50.0% | 14.0 s |
| 20 | 2 | 5 | 89.6% | 13.8 s |
| 20 | 3 | 8 | 85.4% | 33.8 s |

## Decision

**Do not adopt it. Record the measurement so the next attempt starts here.**

Four reasons, in order of weight.

**It rescues nothing.** Across every broken case measured, WPE moved none of them below the
15% word error this project treats as usable. 87.5% becomes 41.7%; that is a large
improvement and an unusable result either way. The technique halves the error and does not
change the outcome.

**The cost does not fit.** The best setting runs at **0.84x real time** for the dereverberation
alone, on top of recognition's measured 0.399x and translation on every final.
`docs/PERFORMANCE_BUDGET.md` already records p50 endpoint-to-caption *at* the 700 ms target
under load. The cheapest useful setting is 0.25x for 54.2%, which is affordable and buys even
less.

**The measurement is an upper bound.** This is offline WPE with the whole utterance available.
Live, the filter would have only the past, and streaming WPE is the weaker variant. Whatever
ships would do worse than 41.7%.

**Sitting closer beats it by forty points, free.** The same room at 0.4 m instead of 1.0 m
measures 2.1% against 87.5%. There is no configuration of this method that competes with
moving 60 cm.

## What this does not do

- **It does not reject WPE for a microphone array.** The method is designed for multiple
  channels and is much stronger there. This project has one microphone, and that is what was
  measured.
- **It does not admit or reject `nara-wpe`** or any package. No dependency was proposed,
  because the measurement did not justify getting that far. Article 12 review of provenance,
  footprint and licence has not been done and is not implied.
- **It does not rule out a dereverberating recogniser.** A model trained on reverberant speech
  would address the same problem inside the acoustic model, at no extra runtime cost, and is a
  different and probably better decision than bolting a front end onto this one.
- **The implementation was mine and unoptimised.** A production implementation would be faster
  — though the 40-tap setting is dominated by the linear solve per frequency bin, which does
  not vectorise away. It would not be enough to change the first reason, which is about
  accuracy rather than speed.

## Consequences

- The chain that began with "why is live recognition sometimes bad" is complete: not the gain,
  not the level, the room — the room is measured, it cannot be detected, and the standard fix
  does not fix it here.
- The honest advice remains what ADR 0027 arrived at: **sit closer, or use a headset.**
  Everything measured since has strengthened that rather than replaced it.
- Anyone proposing dereverberation later has a baseline, a parameter sweep and a cost to beat,
  rather than starting from an intuition that it ought to help.

## Review trigger

When a recogniser trained on reverberant speech is considered, which addresses this at a
better layer; or if hardware with two microphones is supported, which is the configuration WPE
is actually designed for.
