# ADR 0027 — The room, not the level

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** LOW — a measurement, and a decision about what to tell the user

## Context

[ADR 0026](0026-accuracy-and-level.md) measured accuracy against signal-to-noise and closed
on an unexplained result: a live run that lost roughly half its words implied an SNR of about
+3 dB, which that experiment put at **0% word error**. Additive noise did not explain what a
live microphone actually does. Reverberation was named as the suspect and left untested.

## Method

Synthetic room impulse responses by the image-source method, convolved with the publisher's
speech sample. Two things make this worth trusting:

- **The responses were validated, not assumed.** RT60 is measured back off each generated
  response by the Schroeder integral rather than taken from what was requested — Sabine's
  estimate is approximate, and asking for 0.5 s produced 0.70 s. Every figure below is the
  measured value.
- **Level is held constant.** Every file is renormalised to the clean sample's rms, so
  reverberation is the only variable. ADR 0026 showed level explains nothing here; this
  removes it from the experiment entirely.

`DRR` is the direct-to-reverberant ratio: the energy arriving straight from the speaker
against everything that arrives by way of a wall.

## Result

48-word sample, so 2.1% per word. Room 5×4×3 m.

| RT60 | distance | DRR | reverb only | reverb + room noise |
| --- | --- | --- | --- | --- |
| 0.44 | 0.5 m | +1.2 dB | 2.1% | 0.0% |
| 0.44 | 1.0 m | −4.9 dB | 4.2% | 18.8% |
| 0.44 | 2.0 m | −8.9 dB | 10.4% | 27.1% |
| 0.74 | 0.5 m | −4.0 dB | 6.2% | 37.5% |
| 0.70 | 1.0 m | −10.3 dB | **52.1%** | **95.8%** |
| 0.69 | 2.0 m | −13.1 dB | 45.8% | 87.5% |

**There is a cliff in DRR, between about −9 dB and −10 dB.** Above it, reverberation costs a
few per cent. Below it, half the words.

**The two mechanisms compound rather than add.** Room noise at this level costs nothing on
its own — ADR 0026 measured 0% word error at this SNR — and reverberation at RT60 0.44 / 1 m
costs 4.2%. Together they cost 18.8%. At RT60 0.70 / 1 m the pair reaches 95.8%. Neither
number predicts the pair.

**And the level moves the wrong way.** The reverberant files measure `rms 0.031`; adding room
noise raises them to `rms 0.048` *and* makes them far worse. Louder is worse here, which is
the sharpest possible restatement of ADR 0026's finding that level cannot be used to judge
whether recognition will work.

## What this explains

A live run that lost about half its words, at a level implying +3 dB SNR. RT60 near 0.7 at
one metre — a normally furnished room, a laptop at arm's length — measures 52.1% on its own.
The room was the mechanism all along, and both this project's earlier explanations were
wrong: not the gain (ADR 0020's territory), and not the level (ADR 0026's).

## Decision

**Say nothing new to the user, and record why.**

The obvious advice — *get closer to the microphone* — is genuinely the most effective action
available. Going from 2.0 m to 0.5 m in the livelier room takes word error from 87.5% to
37.5%, and in the quieter room from 27.1% to 0.0%. Nothing else on this list is close.

But there is no way to know when to say it. DRR is not derivable from the statistics
`LevelMonitor` holds; the reverberant files are *quieter* than the ones that work, and the
verdict reads `ok` on every row here. Estimating DRR needs a different measurement — blind
reverberation estimation from the decay of speech offsets, or the recogniser's own
confidence — and neither exists in this project. Advice that cannot be timed is worse than
none, for the same reason ADR 0026 declined a quiet-side threshold.

What is written down instead is that **a close-talking microphone or headset solves this
completely**, since DRR is a function of distance, and that a laptop's built-in microphone at
arm's length in a normal room is close to the cliff.

## What this does not do

- **It does not measure a real room.** These are synthetic impulse responses from an idealised
  shoebox with uniform absorption. Real rooms have furniture, non-uniform walls and a
  frequency-dependent RT60. The shape of the result is trustworthy; the exact numbers are not
  the reference machine's actual room.
- **It does not test dereverberation.** WPE and similar are the standard answer and would be a
  dependency-admission decision under Article 12.
- **It does not test a headset**, which is the recommendation this ADR makes. Nobody here has
  one to measure.
- **One speaker, one language, one sample, one room geometry.**

## Consequences

- The chain of explanations for poor live recognition is now closed: not the gain, not the
  level, the room.
- The `README` states that a built-in microphone at arm's length in a normal room sits near a
  cliff, and that sitting closer is the single most effective thing a user can do.
- ADR 0026's review trigger — "when a room with reverberation can be measured properly" — is
  satisfied for synthetic rooms and still open for real ones.

## Review trigger

When a blind DRR or reverberation estimate is available, which is what would make the advice
timeable; or when dereverberation is considered as a dependency; or when a real measured
impulse response can be used instead of a synthetic one.
