# ADR 0024 — A silence rule that fired twice in thirty seconds

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — changes where every live utterance is cut

## Context

ADR 0022 bounded run-on utterances with a ceiling and said plainly what it was not doing:

> **`rule2` is deliberately not tuned**, though it is the rule that governs everyday
> responsiveness. Every value from 0.4 to 1.2 produces byte-identical output on both
> samples, because LibriSpeech clips are trimmed and contain no internal pause longer than
> 0.4 s. There is nothing here to calibrate against.

ADR 0023 then made live endpointing observable, and a live run showed the ceiling doing the
work the silence rule should have been doing: one utterance ended at `8.16s MAX_DURATION`,
the next at `7.68s SILENCE`. Eight seconds between translations is not conversation.

## The measurement

`scripts/measure_pauses.py` listens, waits for the first word, and records how long the
silences between speech last. **It keeps no audio** — durations and a per-second summary,
which is what makes calibrating this possible at all without retaining someone's speech.

Thirty seconds of conversational speech, 14.1 s of it speaking, 75 pauses:

```text
p50 0.12s   p75 0.22s   p90 0.56s   longest 1.34s

threshold   fires on            resulting cadence
   1.2s      2 of 75 ( 2.7%)    one utterance per 15.0s
   0.8s      4 of 75 ( 5.3%)    one utterance per  7.5s
   0.6s      7 of 75 ( 9.3%)    one utterance per  4.3s
   0.5s      9 of 75 (12.0%)    one utterance per  3.3s
   0.4s     12 of 75 (16.0%)    one utterance per  2.5s
   0.3s     14 of 75 (18.7%)    one utterance per  2.1s
```

**At the publisher's default the rule fires on two pauses out of seventy-five.** It is
effectively off for natural speech, which is why the ceiling was cutting everything.

The pauses are two populations. Gaps *inside* speech cluster at 0.12–0.22 s, then there is a
jump to sentence boundaries from about 0.56 s. A threshold belongs in the space between
them.

## Decision

**`SILENCE_AFTER_SPEECH_SECONDS` = 0.5**, down from the publisher's 1.2.

Above the within-speech cluster by more than a factor of two, below the boundary population
it is meant to catch, and a cadence of about one utterance every 3.3 s — which is roughly
one sentence, and roughly what a person waits before expecting an answer.

Not a rounder number, because 0.6 catches fewer of the boundaries (7 of 75 against 9) for no
gain and 0.4 starts reaching into the within-speech cluster.

### It also fixes the mid-word cuts

Unexpected, and measured. Three copies of the published sample — which carries 0.86 s of
trailing silence, so it is a speaker who pauses between sentences:

| | utterances |
| --- | --- |
| 1.2 s (before) | 20w, 22w, 13w — the ceiling cut, and cut inside words |
| **0.5 s (after)** | **18w, 18w, 18w — three whole sentences, ceiling never fires** |

Each repetition is now recognised as the complete sentence it is, identical to the
single-file transcript. ADR 0022 accepted mid-word cuts as the price of bounding utterance
length. Most of that price was being paid because the silence rule was not doing its job.

## What this does not do

- **It does not remove the ceiling.** It stays at 8 s for a speaker who genuinely does not
  pause, and it still cuts wherever the clock lands when it fires.
- **It is calibrated on one speaker.** Seventy-five pauses from one person, one language,
  one sitting. Pause structure varies by speaker, language and how animated a conversation
  is, and nothing here establishes that 0.5 s is right for anybody else.
- **It does not touch `rule1`** (2.4 s, silence before anything is decoded), which governs
  the gaps between bursts rather than inside them.
- **It has not been confirmed live.** The measurement is live; the resulting value is
  verified against recordings only.

## Consequences

- Translations arrive about every 3.3 s of speech rather than every 8 s.
- Utterances more often correspond to sentences, which is also better input for the
  translator — ADR 0009's argument for translating finals rather than partials assumed
  finals were sentence-shaped, and until now they were clock-shaped.
- `scripts/measure_pauses.py` is in the repository, so the number can be re-derived rather
  than trusted. It exists because the honest alternative — recording someone and analysing
  the file — would retain speech in order to tune a parameter.
- A test asserts the value stays in the range the measurement supports, so raising it back
  toward the publisher's default fails rather than quietly returning to run-ons.

## Review trigger

When a second speaker, or a second language, can be measured — the value rests on one
person's pause structure. Also if `listen` starts showing utterances that end mid-sentence,
which would mean 0.5 s is reaching into the within-speech cluster for that speaker.
