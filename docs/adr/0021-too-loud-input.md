# ADR 0021 — Clipping was the wrong thing to measure

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — changes what the product tells a user about their own hardware

## Context

ADR 0019 added a level check so that a microphone producing unusable audio would say so
rather than being transcribed. ADR 0020 left one gap in it, and `listen` then reproduced the
gap live: **eight seconds of an empty room, recognised as `IN` and `EVERY`, with `input ok`
printed beside them.** Two finalised words, nobody speaking, and a check whose entire purpose
was to prevent exactly that reporting no problem.

The obvious reading is that the input was too loud and the threshold was too high. That
turns out to be wrong in an instructive way.

## Over-gain does not break recognition

The same recording ADR 0019 calibrated against — `test_wavs/0.wav` from the pinned English
model's own release, which measures peak 0.535 and rms 0.0471 — amplified digitally and run
through the real streaming recogniser. Word error is against the transcript at gain 1:

| gain | peak | rms | clipped | verdict | WER |
| --- | --- | --- | --- | --- | --- |
| x1 | 0.535 | 0.047 | 0.00% | ok | 0.0% |
| x4 | 1.000 | 0.185 | 0.29% | ok | 0.0% |
| x8 | 1.000 | 0.326 | 3.20% | ok | 0.0% |
| x12 | 1.000 | 0.420 | 7.93% | clipping | 0.0% |
| x24 | 1.000 | 0.569 | 21.30% | clipping | **0.0%** |
| x32 | 1.000 | 0.622 | 27.74% | clipping | 5.6% |
| x48 | 1.000 | 0.688 | 37.24% | clipping | 5.6% |

**Speech with a fifth of its samples pinned at full scale still transcribes word for word.**
The recogniser is far more tolerant of distortion than the ADR 0019 threshold assumes, and
the existing `CLIPPING` warning spends most of its firing range being technically true and
practically irrelevant.

So the failure is not distortion. What produced `IN` and `EVERY` was an amplified *room* —
a noise floor lifted to speech-like energy, with nothing being said in it.

## The two cases are numerically identical

| | peak | rms | crest |
| --- | --- | --- | --- |
| speech at x12 (transcribes perfectly) | 1.000 | 0.420 | 2.4 |
| an empty room at +60 dB (invents words) | 1.000 | 0.409 | 2.4 |

Peak, rms and crest factor cannot separate them, because there is nothing instantaneous to
separate. **The difference is over time: speech has pauses and a room does not.**

Measured as the quietest tenth of a window — the level the input falls back to between
whatever is happening in it:

| input | floor |
| --- | --- |
| speech x8 | 0.042 |
| speech x12 | 0.063 |
| speech x24 (0.0% WER) | 0.122 |
| speech x32 (5.6% WER) | 0.156 |
| empty room at +60 dB | **0.333** |
| empty room at +30 dB | 0.009 |

The speech figures are from continuous stretches with leading and trailing silence removed,
so they are the adversarial case: somebody talking without stopping, not a recording
flattered by its own silence.

## The window has to be long

At one second the separation disappears entirely — heavily amplified speech reads *higher*
than the broken room, because a one-second window can sit inside a single word:

| window | speech x24 | empty room at +60 dB |
| --- | --- | --- |
| 1 s | 0.450 | 0.427 |
| 2 s | 0.186 | 0.398 |
| 5 s | **0.122** | **0.333** |

Five seconds is the shortest window that reliably contains a pause.

## Decision

**A fifth verdict, `TOO_LOUD`, when the quietest tenth of the last five seconds is at or
above 0.15 of full scale.**

0.15 sits between the loudest gain that still transcribes perfectly (x24, floor 0.122) and
the first that does not (x32, floor 0.156), and a factor of 2.2 below the input that
invented words. It is placed where recognition measurably begins to fail, rather than where
the numbers look tidy.

The verdict is withheld until a full five-second window exists. A message accusing someone's
microphone should not be reachable from two seconds of audio, and the separation it depends
on does not exist at that length.

`CLIPPING` is still reported first: it is the more specific statement about the same
problem, and it can be made from one second rather than five.

## What this does not do

- **It does not lower the clipping threshold.** The measurements above argue for leaving it
  alone or raising it — 5% of samples at full scale costs nothing detectable in accuracy.
  Raising it is a separate change with its own evidence, and quietly widening the tolerance
  in a decision about something else is how thresholds stop meaning anything.
- **It does not detect hallucination.** It detects the input condition that produced it
  here. A recogniser inventing words from *quiet* noise would still not be caught, and
  nothing in this project currently could.
- **It does not distinguish a loud room from loud machinery, a fan, or music.** All of them
  are continuous non-speech energy, and the advice — turn the gain down — is the same.
- **It still has not been tested against live speech.** Every speech figure here is
  digitally amplified from a recording. Nobody has yet spoken into a microphone under
  measurement, so the false-alarm rate on real live speech is reasoned about rather than
  observed. That is the remaining gap, and it is the same one ADR 0019 and ADR 0020 left.

## Consequences

- The reference machine at its as-found gain now reports
  `too_loud (peak 1.00, rms 0.445, clipped 0.9%, floor 0.373)` and tells the user to turn
  the gain down. It previously reported `ok` while transcribing an empty room. Note the
  0.9%: the clipping check was never going to catch this.
- `LevelReading` carries a `floor` alongside peak, rms and clipped fraction. It is one more
  number and no audio, `OPERATIONAL_METADATA` exactly as the others are.
- `LevelMonitor` holds a second, longer window of one float per frame. The verdict costs a
  sort of 250 floats per frame, which is immaterial against recognition.
- The window and the command line need no changes: both render whatever advice a verdict
  carries, so a new one appears in both by existing.

## Review trigger

When live speech can be measured, which is the first chance to observe a false-alarm rate
rather than argue one; or if the clipping threshold is revisited on the evidence above.
