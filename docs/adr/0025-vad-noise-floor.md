# ADR 0025 — A detector that deafened itself

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — changes where every utterance on the batch path is cut

## Context

Found by accident. An instrument written to measure live pauses (ADR 0024) used
`EnergyVoiceActivityDetector` and reported `0.0s of speech in 30.0s` while somebody was
talking into the microphone.

The detector seeds its noise floor from its first frame:

```python
if not self._seeded:
    self._noise_floor = rms
    self._seeded = True
```

Seeded on speech — anyone who starts the application already talking — the floor lands three
times too high and nothing the speaker says clears the threshold.

**And it could not recover**, which is the more interesting half. The floor adapted only on
frames classified as silence, at 5% per frame, in both directions. A frame the detector
missed was treated as silence and pulled the floor *toward speech level*, so missing one
frame made missing the next more likely. The failure fed itself.

## Measurement

Scored against a reference labelling of the same audio, on the publisher's speech samples,
starting capture at four different offsets. F1, so a detector cannot win by saying "speech"
more often:

| | `en-0` @0 | `en-0` mid | `en-1` @0 | `en-1` mid |
| --- | --- | --- | --- | --- |
| symmetric (before) | **95.8%** | 13.4% | **31.8%** | 17.5% |
| fall fast, rise slowly | 92.9% | 95.7% | 94.2% | 93.7% |

Two things are worth reading twice. The old detector scores **31.8% on continuous speech
from a clean start** — this was never only about starting mid-sentence; sixteen seconds of
someone not pausing is enough to deafen it on its own. And it scores 95.8% in exactly one
case: the short sample, from the beginning, which is the case its tests used.

## Decision

**Let the noise floor fall fast and rise slowly.**

```text
DEFAULT_ADAPTATION_RATE = 0.05   upward, unchanged
DEFAULT_RECOVERY_RATE   = 0.5    downward, new
```

A frame quieter than the floor is evidence the floor is wrong and the room is quieter than
believed; that evidence should be acted on immediately. A frame louder than the floor may be
the speech this detector exists to find, so it is admitted slowly or not at all. The
asymmetry is the whole fix.

Fast rather than instant, so a single anomalously quiet frame — a dropout, a glitch — moves
the floor halfway rather than all the way. Measured, instant and 0.5 are within a point of
each other overall, and 0.5 is the more forgiving.

A `recovery_rate` below `adaptation_rate` is refused at construction: reversed, the detector
deafens itself faster than it recovers, which is precisely the defect.

## What this does not do

- **It does not change the seeding.** Seeding from the first frame is still wrong when that
  frame is speech; it is now *survivable*, because the first quiet moment corrects it. A
  priming window was measured as an alternative and was worse in every condition
  (34–91% F1 against 84–98%), because it has to guess how long to wait.
- **It costs 2.9 points in the one case the old behaviour was good at** — the short sample
  from a silent start, 95.8% to 92.9%. That is the trade for 60–80 points everywhere else.
- **It does not affect live translation.** `listen` and `stream` use the transducer's own
  endpointing and never touch this detector (ADR 0008). The batch path — `segment` and
  `transcribe` — is what was broken.
- **It is still an energy detector.** It cannot tell a voice from a slammed door, and Silero
  remains the intended replacement under Article 12.

## Consequences

- `segment` and `transcribe` find utterances they previously missed, and someone who starts
  the application mid-sentence is heard.
- The tests that covered this detector passed throughout, because they fed it room tone and
  then speech — the one ordering the old code handled. The new tests start mid-speech, run
  continuous speech, and assert the asymmetry directly.
- `scripts/measure_pauses.py` deliberately does **not** use this detector, and says why: an
  instrument must not share a defect with the thing it measures. That stays true after the
  fix, since a fixed threshold is the right choice for a calibration tool regardless.

## Review trigger

When Silero VAD is admitted, which replaces this entirely; or if a room is found where a
falling floor makes an idle microphone audible — the absolute silence floor is what bounds
that, and it is untouched here.
