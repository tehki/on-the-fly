# ADR 0028 — The obvious estimator, built and rejected

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** LOW — a measurement, and a decision not to ship something that tested well

## Context

[ADR 0027](0027-reverberation.md) established that reverberation, not level, drives live
recognition accuracy, and that the useful advice — *sit closer, or use a headset* — could not
be given because there was no way to know when to give it. Its review trigger asked for a
blind estimate of direct-to-reverberant ratio.

There is an obvious candidate, computable from numbers `LevelMonitor` already holds.
Reverberation fills the gaps between words, so the ratio of the noise floor to the overall
level — `floor / rms`, where `floor` is the quietest tenth of a five-second window
([ADR 0021](0021-too-loud-input.md)) — should rise as a room gets livelier.

It does. That is the problem.

## It works on synthetic audio

Twenty-five room configurations, five reverberation times by five distances, each convolved
with the publisher's 48-word sample at constant level, plus the nine noise mixtures from
[ADR 0026](0026-accuracy-and-level.md). Thirty-four points, "broken" defined as word error
above 15%:

| statistic | best threshold | misclassified |
| --- | --- | --- |
| DRR (not blindly observable) | −10.45 dB | 2 of 25 |
| **`floor/rms`** | **0.449** | **4 of 34, none missed** |
| RT60 | 0.595 | 4 of 25 |

Four false alarms and **zero missed detections** is a good profile for advice: it never
stays quiet while recognition is broken, and occasionally suggests moving closer to somebody
who did not need to. On this evidence it looked ready to ship.

## It fails on real audio

Five live runs recorded during the same session, through the same microphone:

| run | `floor/rms` | what happened |
| --- | --- | --- |
| reading aloud | 0.227 | substantially correct |
| ninety-second run | 0.247 | substantially correct |
| instrumented run | **0.222** | roughly half the words wrong |
| conversational run | 0.333 | badly garbled |
| nobody speaking | **0.765** | an empty room |

**The worst real run scores lower than the best**, and the highest score of all belongs to a
room with nobody in it. Any threshold that catches the garbled run at 0.333 fires on both
good runs on the synthetic scale, and no threshold separates 0.222 from 0.247 at all.

The reason is that the statistic does not measure reverberation. It measures how much energy
sits in the gaps, which rises when a room is live, and when it is noisy, and — decisively —
when there is no speech to make the gaps deep in the first place. An empty room is all gap.
On synthetic data, where the only variable was reverberation, that confound could not appear.

## Decision

**No verdict, and the approach is foreclosed rather than left open.**

`floor/rms` is not a reverberation estimate and must not be used as one. ADR 0027's decision
to say nothing stands, now for a stronger reason than "nobody has tried": the obvious thing
was tried, it validated well, and it does not work.

## What this does not do

- **It does not rule out blind DRR estimation**, which is a real technique — from the decay
  of speech offsets, or from the coherence between channels on hardware that has two
  microphones. Neither is a threshold on numbers this project already computes, which is what
  was being tested here.
- **It does not invalidate ADR 0027.** The DRR row above is from the same grid and confirms
  it at twenty-five points rather than six: DRR predicts accuracy well. It is the *blind*
  part that fails.

## The part worth remembering

A statistic was fitted on thirty-four points and scored 88%, with the errors all on the safe
side. It failed on the first five real examples it met, and inverted on two of them. The
synthetic set had exactly one thing wrong with it — every sample contained speech — and that
was enough.

Nothing in this project's rules would have caught that. `make check` passes either way;
Article 2 and handbook 52 require that a claim be tested, and this claim *was* tested. What
they do not require is that the test resemble the world. **A validation set assembled by the
same reasoning as the feature will confirm the feature.**

## Review trigger

When a genuine blind DRR estimator is implemented, or when two-microphone hardware makes
coherence-based estimation possible. Any future candidate is to be tested against the five
live readings above before any synthetic set.
