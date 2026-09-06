# ADR 0026 — Level cannot tell you the microphone is too far away

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** LOW — a measurement and a decision not to build something

## Context

Live runs on the reference machine varied wildly in quality: one read passage came back
substantially correct, another from the same speaker came back badly garbled. The obvious
explanation was input level, since the good run measured `rms 0.089` and the poor one
`0.048`. ADR 0021 had just added a verdict for input that is *too loud*; the apparent
symmetry invited a matching verdict for input that is too quiet.

That invitation should be declined, and the measurements say why.

## Accuracy against signal-to-noise

Attenuating a recording is not a model of sitting further from a microphone — it lowers the
voice and the room together and leaves the ratio between them untouched. So the voice was
attenuated against **this room's own recorded noise**, held constant, and each mixture run
through the real recogniser. Word error is against the transcript of the clean sample.

| voice | speech rms | SNR | rms as measured | verdict | WER |
| --- | --- | --- | --- | --- | --- |
| 1.00 | 0.0471 | +4.5 dB | 0.0563 | ok | **0.0%** |
| 0.70 | 0.0330 | +1.4 dB | 0.0451 | ok | 0.0% |
| 0.50 | 0.0235 | −1.5 dB | 0.0387 | ok | 5.6% |
| 0.35 | 0.0165 | −4.6 dB | 0.0348 | ok | 5.6% |
| 0.25 | 0.0118 | −7.5 dB | 0.0328 | ok | 11.1% |
| 0.18 | 0.0085 | −10.4 dB | 0.0318 | ok | 33.3% |
| 0.12 | 0.0057 | −13.9 dB | 0.0311 | ok | **100%** |
| 0.05 | 0.0024 | −21.5 dB | 0.0307 | ok | 100% |

**The recogniser is far more robust than expected** — usable to about −7 dB, degrading to
−10 dB, and only then collapsing. Below the cliff it emits **nothing at all** rather than
inventing words, which is the opposite of the over-gain failure in ADR 0021 and much the
safer of the two: a user who speaks and sees no caption knows something is wrong.

**And the measured level barely moves.** From word-perfect to total failure, rms travels
from 0.0563 to 0.0307 — a factor of 1.8 — because once the voice is quiet the room is what
is being measured. `input ok` is printed on every row, including the ones that recognise
nothing.

## Nothing this project computes separates them

Every statistic `LevelMonitor` holds, against the same rows:

| voice | rms | peak | floor | peak/floor | rms/floor | WER |
| --- | --- | --- | --- | --- | --- | --- |
| 1.00 | 0.0563 | 0.54 | 0.0177 | 30.4 | 3.19 | 0.0% |
| 0.50 | 0.0387 | 0.31 | 0.0156 | 19.9 | 2.48 | 5.6% |
| 0.18 | 0.0318 | 0.31 | 0.0144 | 21.5 | 2.20 | 33.3% |
| 0.12 | 0.0311 | 0.31 | 0.0142 | 21.8 | **2.19** | 100% |
| 0.05 | 0.0307 | 0.31 | 0.0139 | 22.3 | 2.21 | 100% |

`peak` pins at 0.31 from halfway down, because it is measuring the loudest thing in the
room and that stops being the voice. `peak/floor` is not even monotonic — 30.4, then
19.9 to 22.3 for everything from mild degradation to complete failure. `rms/floor` is the
best of them and still puts 33% WER at 2.20 and 100% WER at 2.19.

**There is no threshold to place.** The failing rows are not distinguishable from the
degraded ones by anything here.

## Decision

**No verdict for quiet or distant input.** The level statistics cannot detect this failure,
and a threshold placed anyway would fire on inputs that work and stay silent on inputs that
do not — worse than saying nothing, because it would be believed.

`SILENT` and `QUIET` stay as they are. They catch a muted device and an input that is quiet
in absolute terms, which are real and different problems.

## The measurement does not explain the live runs

Stated plainly because it undermines the tidy story. The garbled live run measured
`rms 0.048` against a room floor of 0.028, which implies a speech component near 0.039 and an
SNR around **+3 dB** — a row this table puts at 0% WER. The observed error rate was roughly
half the words.

So additive noise at a matched SNR is *much* easier than real distance. What this experiment
cannot reproduce is what a room does to a voice on its way to a microphone — reverberation,
early reflections, the microphone's own response off-axis — and that appears to matter more
than the noise it adds. The earlier conclusion in this session that live accuracy was "about
level" is not supported; level is a correlate of distance, not the mechanism.

## What this does not do

- **It does not measure live accuracy.** Every number here is a digital mixture. The live
  word error rates quoted in this session are impressions from comparing a transcript against
  a message that was read aloud, not a scored measurement, and they are not repeated as facts
  anywhere in this repository.
- **It does not rule out a usable detector.** A speech-band SNR estimate, or the recogniser's
  own lattice confidence, might well separate these rows. Neither is computed today and
  neither is a threshold on the numbers this project already has.
- **One speaker, one room, one microphone, one language.**

## Consequences

- The quiet-side gap noted in ADR 0021 is closed as *measured and not actionable*, rather
  than remaining an open invitation to add a mirror-image threshold.
- The failure mode is at least benign: below the cliff the pipeline goes silent rather than
  producing confident nonsense, so the dangerous direction remains the loud one.
- Anyone tempted to read `input ok` as "recognition is working" now has a table showing it
  printed beside 100% word error.

## Review trigger

When a speech-band SNR estimate or a recogniser confidence score is available — either could
detect what frame energy cannot; or when a room with reverberation can be measured properly,
which is what actually appears to drive live accuracy.
