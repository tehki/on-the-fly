# ADR 0022 — Utterances that never end, from a number in the wrong unit

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — changes where the recogniser cuts a speaker

## Context

The first live speech this project ever recognised (2026-09-06, thirty seconds read aloud)
came back as **two finals**. The first covered roughly fourteen seconds. Its translation
arrived in a single lump after the speaker had stopped, and it was poor — the translator was
handed thirty words with no punctuation and no boundaries.

That is not what a live translator does. Reproduced immediately on a recording, so it is not
a property of one person's reading voice: `en-1.wav` — 16.7 seconds of continuous read
speech — produced **one final of 48 words**.

## Cause

```python
rule3_min_utterance_length = (300,)
```

`rule3_min_utterance_length` is in **seconds**, and sherpa-onnx defaults it to 20. This file
passed 300, which reads perfectly well as a frame count and is five minutes as a duration.
The ceiling could therefore never fire, leaving `rule2_min_trailing_silence` — 1.2 seconds of
silence — as the only way an utterance could ever end. Nobody reading aloud pauses that long.

Nothing failed, nothing logged, and no test noticed, because every recording the project
tests with is a single utterance that ends when the file does. It took a live microphone and
a person who did not stop talking.

## What the ceiling costs

The ceiling cuts where the clock says, not where a word ends. Measured on both samples,
comparing the joined transcript against the same audio with the ceiling disabled:

| ceiling | `en-0` (6.6 s) | `en-1` (16.7 s) |
| --- | --- | --- |
| 10 s | 1 final, identical | 2 finals, identical |
| **8 s** | **1 final, identical** | **3 finals, identical** |
| 6 s | 2 finals — `BROTHEL` split into `BRO` + `THEL` | 3 finals, identical |
| 4 s | 2 finals, identical | 4 finals — `PUNISHED` split into `PUNISH` + `ED` |

**Eight seconds is the shortest value that split no word in either sample.** Lower ceilings
fire more often and therefore land mid-word more often; there is no value at which that risk
is zero, because a clock knows nothing about syllables.

## Decision

**Pass the three endpoint rules as named constants, in seconds, and set the ceiling to 8.0.**

```text
SILENCE_BEFORE_ANY_SPEECH_SECONDS = 2.4    publisher default, untouched
SILENCE_AFTER_SPEECH_SECONDS      = 1.2    publisher default, untouched
MAX_UTTERANCE_SECONDS             = 8.0    was 300
```

Named, because the failure was silent and a bare `300` next to a bare `1.2` gives a reader
nothing to check against. A test asserts the ceiling is in a range only a duration in seconds
could be in.

**`rule2` is deliberately not tuned**, though it is the rule that governs everyday
responsiveness. Every value from 0.4 to 1.2 produces byte-identical output on both samples,
because LibriSpeech clips are trimmed and contain no internal pause longer than 0.4 s. There
is nothing here to calibrate against, and picking a number that no measurement can
distinguish is how 300 got written in the first place.

## What this does not do

- **It does not stop utterances being cut mid-word.** It makes it rarer, at the value tested.
  Cutting at a quiet moment instead of at a fixed time would be the real fix, and needs
  speech with pauses in it to develop against.
- **It does not improve the translation of a run-on.** It bounds how long one can get.
- **It has not been re-tested against live speech.** The fix is verified on recordings and on
  a repeated-sample test; nobody has spoken into a microphone since it landed.
- **It does not touch the two silence rules**, so an ordinary sentence still ends exactly
  where it did before.

## Consequences

- A speaker who does not pause now gets a caption and a translation at least every eight
  seconds instead of whenever they stop. The caption itself was never frozen — partials
  stream throughout — so what changes is when the *translation* arrives.
- `docs/PERFORMANCE_BUDGET.md` gains the metric this exposed. It measures endpoint → caption
  and had no target for how long the endpoint itself takes to arrive, so a fourteen-second
  wait was invisible to it: every published figure in that document was measured on
  recordings whose utterances end when the file does.
- A test now feeds the recogniser continuous speech with no pauses in it and asserts more
  than one utterance comes out. It skips when the model is absent, like its neighbours.

## Review trigger

When speech with natural pauses can be recorded, which is what both `rule2` and a
cut-at-silence ceiling would have to be calibrated against.
