# ADR 0041 — A larger Whisper, measured

**Status:** Accepted. **Answers a question [ADR 0035](0035-the-batch-tier-measured.md) left
open and declines the answer it guessed at.**
**Date:** 2026-09-09
**Deciders:** @tehki
**Risk:** MODERATE — changes the default model every `transcribe` run downloads and uses

## Context

ADR 0035 measured Whisper `tiny`, found 77.1% word error on clean read French — *"not a
transcript with mistakes in it, a different sentence"* — and said what it could not say:

> **It does not condemn Whisper.** It measures `tiny`, the 78 MB model this project pins to
> prove the pipeline on a CPU. A larger Whisper would very likely score far better and would
> be a different pin, a different download and a different latency decision.

Four of the seven supported languages have nothing but this path. That sentence is the whole
of the batch tier's hope, and it was an expectation rather than a measurement.

## Evidence

`base` and `small` pinned from the same publisher, same licence, same four files. Measured on
both published test sets this project holds human references for, with the same word error
implementation, the same normalisation and the same `--language` forced:

| | English, 66 words | French, 35 words |
| --- | --- | --- |
| `tiny` | 6.1% | 68.6% |
| **`base`** | **4.5%** | **54.3%** |
| `small` | 4.5% | **34.3%** |
| the pinned *streaming* model | 0.0% | 14.3% |

**English is already solved and the remaining errors are not recognition.** At `base` and
`small` all three are the orthography ADR 0035 identified — `dishonoured`/`dishonored` and
`FOR EVER`/`forever`, which the metric counts twice — and `PARENT`, which `tiny` heard as
`parrot`, is now correct. On this sample the honest English figure for `base` is **zero
genuine misrecognitions**.

**French improves by half and is still not usable.** `small` returns a legible transcript of
the right sentence where `tiny` returned a different one:

```text
reference   SON ACTIONNAIRE MAJORITAIRE EST LE CONSEIL TERRITORIAL DE SAINT PIERRE ET MIQUELON
tiny        Sune n'a qu'une est armée j'ai été à l'aluconcéité à la taille de Saint-Pierre
            et Miquelon.
small       Son actionnaire majoritaire et le conseil territorial de Saint-Pierre et
            Michel-Homme
```

Discounting the conventions the metric does not normalise — `QUATRE`/`4`, `SEPT`/`7`,
`SAINT PIERRE`/`Saint-Pierre` — `small`'s honest French figure is around **27%**. Still well
above the 15% line, and still four times the pinned French streaming model on the same clips.

**The speed is the surprise, in both directions.** Real time factor on this machine, one
thread, load average 3:

| | English | French |
| --- | --- | --- |
| `tiny` | 0.33x | **2.79x** |
| `base` | 0.69x | **1.79x** |
| `small` | 2.74x | 5.07x |

`base` is **faster than `tiny` on French**, because a model that returns a different sentence
spends longer returning it: `tiny` decodes a hallucination it then has to finish. `small` is
5x real time — a six-second utterance takes thirty seconds — which is not "several seconds
behind" in any usable sense.

## Decision

**`base` becomes the default. `tiny` and `small` stay pinned and selectable.**

`base` is better than `tiny` on both languages measured and, on the one where it matters,
also faster. It costs 148 MB against 78 MB on a first run. `small` is pinned because a user
willing to wait 5x real time for a legible French transcript should be able to have one, and
because the measurement above should be repeatable by anyone.

**The hope ADR 0035 recorded is not supported.** A larger Whisper does score far better — and
for French it is still not good enough to call the batch tier a translator's input. The four
batch languages remain languages this project transcribes rather than languages it serves.

## And the picker was promising what it could not serve

`--model` offered every pin in the registry. `transcribe --model streaming-en` was an accepted
argument that fetched and verified a 73 MB streaming model and then failed inside CTranslate2
with `Unable to open file 'model.bin'`. That is exactly the defect
[ADR 0034](0034-tiers-describe-this-project.md) removed from the window's language pickers,
still standing in the command line. The choices now come from `batch_pins()`, the complement
of `streaming_pins()` by the same one rule, so the two cannot drift apart.

## What this does not establish

- **It still does not measure Spanish, Italian, Portuguese or German.** No licence-clean test
  set with human references was found for them, which is the wall ADR 0031, ADR 0034 and ADR
  0035 all hit. Two languages are measured here; the other four are not, and ADR 0035's
  withdrawn inference is not being re-made.
- **Three clips is three clips.** The French figure moves by 9 points between ADR 0035's run
  and this one for `tiny` alone (77.1% against 68.6%, the difference being that this run
  forces `--language fr` where that one let Whisper detect it). Both are small samples of the
  same three files, and neither supports a decimal place.
- **It does not make the batch tier live.** `base` at 1.79x real time on French is still
  slower than the audio it is transcribing.
- **It does not change the streaming tier.** Nothing here touches the three pinned streaming
  models, which remain four to fourteen times better on the same clips.

## Review trigger

When a licence-clean test set exists for any of the four batch languages — then the question
this ADR answers for French can be asked for a language that actually depends on the answer.
