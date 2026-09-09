# ADR 0042 — The first thing here that can notice fluent nonsense

**Status:** Accepted. **Answers a gap [ADR 0021](0021-too-loud-input.md) recorded as
unclosable with what this project then had.**
**Date:** 2026-09-09
**Deciders:** @tehki
**Risk:** MODERATE — adds a warning a user will act on, so a wrong one costs trust

## Context

This project has been circling one failure since ADR 0019: **bad input does not produce
silence or an error, it produces fluent words nobody said.** Level checks catch bad *audio* —
a microphone too loud, a room amplified into speech-like energy — and ADR 0021 was explicit
that they do not catch the other half:

> **It does not detect hallucination.** It detects the input condition that produced it here.
> A recogniser inventing words from *quiet* noise would still not be caught, and nothing in
> this project currently could.

[ADR 0041](0041-a-larger-whisper.md) then produced the failure in its purest form, on good
audio, with the level check reporting `ok`:

```text
reference   SON ACTIONNAIRE MAJORITAIRE EST LE CONSEIL TERRITORIAL DE SAINT PIERRE ET MIQUELON
base        S'il y a un accénère majeur littéra le conseil de la taille de 100 pierres et mes culons.
translated  If there is a major accénère littéra the council of the size of 100 stones and my culons.
```

The pipeline did what it is built to do and translated it. Nothing anywhere said the
recogniser had no idea.

## The signal was already there and was being discarded

faster-whisper decodes greedily, then applies its own quality checks — a log-probability
floor and a compression-ratio ceiling — and when they reject the result it **retries the
segment at a higher temperature**, which means sampling rather than taking the best token. The
temperature that was actually used comes back on every segment, and this project was throwing
it away along with `avg_logprob` and `no_speech_prob`.

**A segment above temperature 0 is the model reporting that it rejected its own first answer.**

## Which signal, measured

Two candidates. Three runs of each of the five published clips this project holds references
for, `base`, `--language` forced:

| clip | comes back as | temperature | `avg_logprob` |
| --- | --- | --- | --- |
| `fr_19364697` | mostly right | **0.0, 0.0, 0.0** | −0.537 every run |
| `fr_19738183` | right | **0.0, 0.0, 0.0** | −0.363 every run |
| `fr_27024649` | **a different sentence** | **1.0, 0.2, 0.2** | −1.019, −0.874, −0.919 |
| `en/0` | right | **0.0, 0.0, 0.0** | −0.221 every run |
| `en/1` | right, 3 segments | **0.0 ×3** | −0.178 every run |

**The log probability is the weaker signal, and it is weaker for a specific reason.** OpenAI's
threshold of −1.0 is the one the library *already applied* before falling back, so the number
that reaches us is whatever the retry settled for — it crossed −1.0 in **one run out of five**
on the clip that is wrong every time. The temperature separates the same clips **five times
out of five**.

## Decision

**Report the model's own verdict, and never hide the text behind it.**

- `FasterWhisperRecognizer.transcribe_with_confidence` returns the text, the duration-weighted
  mean `avg_logprob`, the highest `no_speech_prob`, and the highest temperature any segment
  needed. `transcribe` still returns a string: the port is unchanged.
- The extra reporting is an **optional capability**, `ConfidenceReporting`, asked for with
  `isinstance`. Widening `SpeechRecognizer` would make every implementation answer a question
  only one of them can — the streaming transducer has nothing comparable to offer.
- One failed segment fails the utterance, however confident the model was about the rest.
- The command line prints the text and then says so:

  ```text
  [   0.00s +6.32s] sur la scénère majeur littéra le conseil de la taille de 100 pierres…
                    ! the model rejected its own first answer here, confidence -0.99;
                      treat this as unrecognised
  ```

  `--json` carries `confidence` and `failed_decode` beside the text.

**The text is shown, not withheld.** A reader who knows the language is a better judge of a
transcript than a threshold is, and a product that silently drops what a model was unsure
about would be hiding its own failures — which is the thing this project keeps refusing to do.
What changes is that the user is no longer alone with a fluent sentence and no reason to
doubt it.

**`None` is not a complaint.** A recogniser that reports nothing has said nothing about its
answer, and treating that as a failure would put a warning on every caption in the product.

## What this does not do

- **It does not cover the streaming tier**, which is where live captions come from. The
  transducer emits no comparable signal, and inventing one is a different piece of work.
  ADR 0021's gap is closed for `transcribe` and open everywhere else.
- **It does not detect wrong-but-confident output.** `fr_19364697` comes back with
  `ACHÉMÉNIDE` as "HMNID" at temperature 0 and is not flagged. What is detected is the model
  rejecting itself, which is a narrower thing than being wrong.
- **It does not measure a false-positive rate.** Five clips, three runs, two languages: every
  correct decode was at temperature 0, which is evidence and not a rate. A clip that is merely
  difficult — a bad microphone, an accent, background noise — may well be flagged while still
  being right, and the wording ("treat this as unrecognised") is deliberately advice rather
  than a verdict.
- **It does not act on the flag.** Nothing is dropped, refused or retried. Translating a
  failed decode still happens, because the alternative is a product that goes quiet without
  saying why.

## Review trigger

When the streaming tier gets a confidence signal, or when a false positive is observed on
audio a human confirms was recognised correctly — the second would make the wording, not the
threshold, the thing to revisit.
