# ADR 0020 — The first second and a half of a cold capture is not audio

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — changes what the pipeline is fed, and corrects a measurement an earlier
decision was built on

## Context

ADR 0019 shipped a level monitor to tell the user when their microphone is unusable, and
recorded the evidence it was calibrated against:

```text
                       peak    rms      clipped samples
recorded speech (en)   0.535   0.0471   0.0000%
recorded speech (ru)   0.500   0.0790   0.0000%
this machine's mic     1.000   0.8134   51.04%
```

From that it concluded the cause was **capture gain pinned at +30 dB** in the system mixer,
driving the room's noise floor into the rails.

The third row is not a measurement of the microphone. It is a measurement of the analog
input powering up.

## What the capture path actually does

Profiled per 100 ms from the moment the stream opens, at the same mixer settings ADR 0019
found, on a device that had been idle for 30 seconds:

```text
   ms      dc   ac_rms   clip%
    0  -0.4212   0.5242  25.69%
  100  -1.0000   0.0000 100.00%
  200  -1.0000   0.0000 100.00%
  300  -0.9993   0.0097  97.81%
  500  -0.9659   0.0910  70.56%
  700  -0.7175   0.3113  16.88%
  900  -0.3989   0.4860   5.00%
 1300  -0.1370   0.3977   0.12%
 1800  -0.0194   0.3948   0.06%
```

`ac_rms` is the root mean square with the DC component removed — the signal, as distinct
from the offset it is sitting on.

For roughly half a second the converter is pinned at exactly the negative rail: DC −1.0,
100% of samples clipped, and **zero** AC content. That is not saturated audio; it is no
audio at all. The offset then decays over about another second as the DC-blocking servo
charges, and the input is centred by 1.8 s.

Four cold starts, each after 30 seconds of idle, all show it. Two captures started seconds
apart do **not**: the second one is clean from the first frame, because the codec is still
powered. `arecord` straight to a WAV file shows the same shape, so this belongs to the
hardware and its driver rather than to anything this project does.

## What that means for ADR 0019

**The steady state at the mixer settings ADR 0019 blamed does not clip at all.** Measured
over seconds 2–7 of the same capture: 0.000%–0.031% of samples at full scale. The 51.04%
came from a measurement short enough to be dominated by the power-up, and so did the
−0.230 DC offset it reported for the default device — which is why probing device 13 gave
−0.008 and the difference was read as a property of the device rather than of when the
reading was taken.

**The mixer is still misconfigured, and the control named was the wrong one.** ADR 0019
quotes `Capture` at 63/63 (+30 dB). There is a second +30 dB in front of it that it does not
mention. Measured across settings, on settled audio only:

| `Internal Mic Boost` | `Capture` | idle-room ac_rms |
| --- | --- | --- |
| 3 (+30 dB) | 63 (+30 dB) | 0.32 |
| 0 (0 dB) | 63 (+30 dB) | 0.011 |
| 0 (0 dB) | 32 (+6.75 dB) | 0.0009 |
| 0 (0 dB) | 16 (−5.25 dB) | 0.0002 |

Turning the boost off drops the level 28x — the +30 dB it contributes — and is what actually
fixes this machine. `Capture` at 63 on its own leaves an idle room at 0.011, below the
quietest speech measured here.

So ADR 0019's advice to the user is right, its diagnosis is half right, and its evidence is
wrong. The half that is wrong matters, because the number it cites as proof of clipping is
the one number in this project that comes from a transient rather than from the thing being
described.

## Decision

**Drop frames until the input stops looking like a power-up, and do it adaptively.**

```text
domain/audio/settling.py   SettlingSource   an AudioSource that withholds until settled
```

Released when a full window of audio holds |DC| < 0.05 of full scale and under 1% of samples
at full scale. Both thresholds sit between what a settled window measures on this hardware
(|DC| ≤ 0.045 over 100 ms of room noise, ≤ 1% clipped) and what the transient measures
(DC 1.0, passing 0.13 as late as 1300 ms).

Adaptive rather than a fixed delay, because the transient is only there on a cold start.
Measured against the real device: **1780 ms discarded cold, 240 ms warm** — 240 ms being one
window, the price of looking before deciding.

> **Caution added 2026-09-07.** Those two durations are derived from `_dropped_bytes` counted
> at `SettlingSource`, which sits *above* `MicrophoneSource` and therefore above its
> resampler. That resampler was emitting 1.19x the audio it was given until it was fixed, so
> both figures are inflated by roughly that much: the real transient is nearer 1500 ms cold,
> and the warm case is one window by construction rather than by measurement.
>
> No constant depends on them. The three-second cap keeps its margin either way, and the
> 240 ms window is a chosen size rather than a measured one. What changes is only the
> accuracy of two numbers this record and the README quote as facts about the hardware.

The wait is capped at three seconds. An input that never settles is passed through with
`gave_up` set, because a microphone this project refuses to pass through is
indistinguishable, to the person holding it, from a broken one.

It sits *under* the level monitor, so the verdict the user is shown is about their
microphone and not about the analog path coming up.

## Retention

Transient frames are measured and **dropped, not buffered** — there is no path from a
`SettlingSource` back to audio that passed through it, and nothing is held for later. The
window holds three counters per frame. `discarded_ms` is a duration.
`docs/RETENTION_POLICY.md` classifies these as `OPERATIONAL_METADATA`; a test asserts the
object holds no bytes.

## What this does not do

- **It does not fix the level verdict.** With the transient removed, this machine's input
  measures ac_rms 0.40 with peak at full scale — five to eight times the rms of recorded
  speech (0.047–0.079) — and `LevelMonitor` calls it `OK`, because it keys on clipped
  samples and this signal does not clip until something is spoken into it. The verdict is
  now correct about the transient and still wrong about the microphone. Calibrating a
  "far too hot" rule needs speech recorded at several gains, which needs a working
  microphone, which is the next change and not this one.

  > **Fixed by [ADR 0021](0021-too-loud-input.md)**, and not in the way this paragraph
  > expected. Reproduced live once `listen` existed: eight seconds of an empty room at
  > `Internal Mic Boost` 3 transcribed as `IN` and `EVERY`, with `input ok` beside them.
  > But the diagnosis above — "far too hot" — turned out to be the wrong one. Speech
  > amplified until a fifth of its samples clip transcribes word for word; what invents
  > words is a room with no pauses in it, and the two are identical in peak, rms and crest
  > factor. The verdict added there keys on whether the input ever goes quiet.
- **It does not fix the gain for the user.** ADR 0019's reasoning stands: their mixer is
  theirs, and they may be in a call on the same device.
- **It does not verify live recognition.** No *speech* has yet been recognised from a live
  microphone — nobody has spoken into one under measurement. What is now true is that the
  recogniser is no longer handed a rail-pinned second and a half at the start of every
  session, and that the capture path has been driven end to end from a terminal.

  > **Done, 2026-09-06.** Live speech was recognised, with `settling 780ms discarded` on that
  > run. The settling this ADR added is on the path every live session now takes.

## Consequences

- A cold capture session yields its first audio about 1.8 s after the microphone opens
  rather than immediately. Against `docs/PERFORMANCE_BUDGET.md`'s three-second
  "application start → ready to listen", this is spent inside a window already dominated by
  model load, and it buys back audio that was worthless.
- The spurious `CLIPPING` warning at the start of every cold session is gone. It was advice
  to turn the gain down, issued on evidence that had nothing to do with the gain, and it
  cleared by itself a second later.
- ADR 0019's evidence table is annotated in place rather than rewritten, so what was
  believed and why remains legible.

## Review trigger

When speech at known gains can be recorded on working hardware, which is what a level
verdict beyond clipping has to be calibrated against; or if capture hardware appears whose
transient outlasts the three-second cap.
