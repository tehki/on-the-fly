# ADR 0043 — The streaming recogniser's confidence, reported and not acted on

**Status:** Accepted. **Answers [ADR 0042](0042-the-model-saying-it-failed.md)'s review
trigger and declines the feature it was reaching for.**
**Date:** 2026-09-09
**Deciders:** @tehki
**Risk:** LOW — adds one reported number and no behaviour

## Context

ADR 0042 gave the batch tier a way to notice that the model had rejected its own answer, and
was explicit about the half it did not reach:

> **It does not cover the streaming tier**, which is where live captions come from. The
> transducer emits no comparable signal, and inventing one is a different piece of work.

That turned out to be wrong in a useful way. sherpa-onnx does report a log probability per
emitted token — `ys_probs` on the recogniser — and this project was discarding it along with
the timestamps beside it.

## What the number separates, and what it does not

Measured 2026-09-09 through the shipped pipeline: five published clips this project holds
human references for, each decoded by its own language's model and by the other one, taking
the **median of the run's finals**, which is the figure a run would report.

| audio | right model | wrong model |
| --- | --- | --- |
| `en/0` | **−0.249** | (nothing emitted) |
| `en/1` | **−0.311** | (nothing emitted) |
| `fr_19364697` | **−0.351** | −0.875 |
| `fr_19738183` | **−0.435** | −1.114 |
| `fr_27024649` | **−0.827** | −1.376 |

Two things are visible and only one of them was expected.

**The wrong model for the audio does score much lower** — and often emits nothing at all,
which is its own kind of answer: the French model produced no tokens whatever on English
speech.

**And the populations touch.** The worst correct run is −0.827 and the best wrong one is
−0.875: a gap of 0.05, on eight runs. The clip responsible is `fr_27024649`, the same clip
Whisper fails on in [ADR 0041](0041-a-larger-whisper.md) — genuinely hard audio, recognised
correctly by the right model, scoring like a mismatched one.

An earlier draft of this change shipped a warning at −0.75, derived from whole-clip means
before the per-final medians were measured. It fired on that clip **with the correct language
selected**, which is exactly the failure a warning must not have: telling a user their French
is not French while transcribing it correctly.

## Decision

**Report the number. Draw no line through it.**

- `TranscriptEvent` carries `confidence` on finals — a mean token log probability — and `None`
  from a recogniser that does not report one, which is not the same as certainty.
- `StreamingStats` collects them and offers a median. The median, not the mean: one hard
  utterance should not drag a good run, and one lucky utterance should not rescue a bad one.
- `stream` and `listen` print one line: `confidence -0.35 median over 3 final(s)`.
- Nothing is refused, flagged or retried on the basis of it.

The number is printed because it is what the next measurement needs and because a user
comparing two runs can watch it move. A threshold on this evidence would be a line drawn
through a gap the sample does not have.

## What this does not do

- **It does not detect the wrong source language**, though it is close to being able to. The
  strongest version of that signal is not the score at all — it is that the French model
  emitted *nothing* on English audio, twice. A run that produces no finals from audible speech
  may be a better wrong-language detector than any threshold, and it is not built here.
- **It does not detect hallucination from noise**, which is ADR 0021's original gap. The
  synthetic noise tried here — white and low-passed, at three levels — produced **no tokens at
  all**, so the case could not be reproduced to measure. The recording that produced `IN` and
  `EVERY` from an amplified empty room no longer exists.
- **It does not cost anything measurable.** The probabilities are already computed by the
  decoder; reading them is a list of floats per final.
- **It is not per-caption.** A number attached to individual captions would decorate whoever
  speaks least clearly, and the measured separation is not there at that resolution.

## Review trigger

When there are more clips — particularly a recording of the noise-recognised-as-speech case,
or one speaker the streaming models find hard while being right. Either would say whether the
overlap above is this sample or the signal.
