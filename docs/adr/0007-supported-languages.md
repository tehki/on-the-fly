# ADR 0007 — Eight languages, at two tiers, because Tajik has no streaming model

**Status:** Accepted
**Date:** 2026-09-02
**Deciders:** @tehki
**Risk:** MODERATE — sets what the product may claim

## Context

ADR 0006 recommended a hybrid recogniser and left one question open: which languages must be
live. The answer is **English, Russian, Tajik, Spanish, Italian, French, Portuguese, German**.

Seven of those are unremarkable. The eighth changes the plan.

## Evidence

Searched 134 published `sherpa-onnx` streaming model repositories on the model hub, and
checked every Tajik option found.

| Language | Streaming models found | Examples |
| --- | --- | --- |
| English | 50 | NeMo FastConformer (80/480/1040 ms), Nemotron 0.6b, Zipformer |
| Russian | 5 | `streaming-t-one-russian-2025-09-08`, `streaming-zipformer-small-ru-vosk` |
| Spanish | 4 | `streaming-zipformer-es-kroko`, `zipformer-streaming-robust-es` |
| French | 3 | `streaming-zipformer-fr-kroko` |
| Italian | 2 | `streaming-zipformer-it-kroko` |
| German | 2 | `streaming-zipformer-de-kroko` |
| Portuguese | 1 | `streaming-zipformer-pt-kroko` |
| **Tajik** | **0** | — |

Zero is not "we did not look hard enough". Tajik is a low-resource Persian variety written
in Cyrillic, and nobody has published a streaming model for it.

### What Tajik does have, and why each option is a problem

| Option | Licence | Verdict |
| --- | --- | --- |
| `facebook/mms-1b-all` — 158 languages including Tajik, 252k downloads | **CC-BY-NC-4.0** | Non-commercial. Incompatible with this repository's Apache-2.0. Exactly the trap ADR 0001 flagged, arriving as predicted. |
| `burhon97/whisper-tajik-finetuned` | **none declared** | No licence is not permission. Legally unusable regardless of quality. |
| `abduaziz/whisper-small-tajik` | Apache-2.0 | Usable, but 16 downloads. Hobby scale, unvalidated by anyone. |
| Base Whisper | MIT | Claims `tg` — verified present in faster-whisper's language codes. Claiming a language is not performing in it, and Tajik sits in Whisper's long tail. |

## Decision

**Two tiers, stated honestly, encoded in `src/on_the_fly/domain/languages.py`.**

- **STREAMING** — English, Russian, Spanish, Italian, French, Portuguese, German. Results
  appear while the speaker is talking, once the sherpa-onnx engine is adopted.
- **BATCH** — Tajik. Recognised an utterance at a time through Whisper, several seconds
  behind, **with accuracy that has not been verified by anyone here**.

The registry is code, not prose, because the distinction has to survive contact with a user
interface. A language list that renders Tajik identically to German would be the application
telling a lie the code helped construct. `Language.note` carries the reason, and
`has_caveat` exists so a UI cannot render the set without deciding what to do about it.

An unknown language code is refused rather than attempted. A recogniser quietly guessing at
a language nobody validated produces confident nonsense, which for a translator is worse
than an error.

## What this does not decide

**Whether Tajik is good enough to ship.** Nobody here has tested Whisper on Tajik speech,
and there is no Tajik audio on this machine to test with. Until someone who reads Tajik
listens to the output, the honest statement is "it attempts Tajik", not "it supports Tajik".

That is a real gap and it should be closed by evidence, not by optimism. The options if base
Whisper proves inadequate:

1. Pin `abduaziz/whisper-small-tajik` (Apache-2.0) after evaluating it — the only
   licence-clean Tajik-specific model found.
2. Fine-tune from open Tajik speech data, which is a project of its own.
3. Tell users plainly that Tajik is best-effort.

Not: use MMS. The licence forbids it, and "it is only for testing" is how licence violations
begin.

## Consequences

- The product may claim live translation for seven languages, and must not claim it for the
  eighth.
- `sherpa-onnx` is confirmed as the streaming engine target: it publishes models for all
  seven, including the `kroko` family covering five of them from one publisher.
- Whisper stays. It is not the live path, but it is the fallback that makes Tajik possible at
  all — so ADR 0005's dependency is not superseded, it is repositioned.
- Portuguese has exactly one published streaming model. Thin, and worth noting before anyone
  depends on it.

## Review trigger

If a streaming Tajik model appears, or if evaluation shows base Whisper's Tajik is unusable.
Also when adding any language: the tier must be established from evidence before the code
claims it.
