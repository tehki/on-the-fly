# ADR 0011 — Russian is a batch language, because its streaming model is unlicensed

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — narrows what one supported language can do
**Amends:** ADR 0007, which listed Russian at the streaming tier

## Context

ADR 0009 delivered English→Russian translation. The obvious next step is the other
direction, so that two people can hold a conversation rather than one person being
understood.

Translating `ru→en` is straightforward — OPUS-MT publishes that pair and it is pinned
alongside `en→ru` in this change. **Recognising Russian speech is not**, and the reason is
worth recording precisely, because a one-line summary of it would be wrong in a way that
sends the next person down the wrong path.

## What was checked

ADR 0008 recorded that `sherpa-onnx-streaming-zipformer-small-ru-vosk-2025-08-16` declares
no licence, and treated Russian as "not yet cleared". That is true and incomplete. Checked
directly, 2026-09-04:

| Model | Licence | Streaming | Usable |
| --- | --- | --- | --- |
| `csukuangfj/sherpa-onnx-streaming-zipformer-small-ru-vosk-2025-08-16` | **none declared** — the repo card has no metadata at all | yes | **no**: no licence is not permission |
| `alphacep/vosk-model-ru` | **apache-2.0**, confirmed via the hub API | **no** | **no**: not for streaming |
| `alphacep/vosk-model-small-ru` | apache-2.0 | not established | not investigated further |

The middle row is the interesting one, and it looked for a while like the answer.
`alphacep/vosk-model-ru` is the upstream publisher of the model the sherpa republication is
built from. It is Apache-2.0. It ships `encoder.int8.onnx`, `decoder.int8.onnx`,
`joiner.int8.onnx` and `lang/tokens.txt` — exactly the four files sherpa-onnx's streaming
transducer loads.

It was downloaded and tried. It fails:

```text
online-zipformer2-transducer-model.cc:InitEncoder:116
    'encoder_dims' does not exist in the metadata
```

And the encoder's own ONNX metadata says why:

```text
comment      = non-streaming zipformer2
model_author = k2-fsa
model_type   = zipformer2
```

It loads without complaint as an **offline** recogniser. So it is a batch model that
happens to share a file layout with the streaming ones — the resemblance is real and
misleading, and only the metadata settles it.

## Decision

**Russian moves from the streaming tier to the batch tier**, with a note saying why.

It is still supported. It is recognised an utterance at a time through Whisper, which is
already pinned and MIT-licensed and does cover Russian, and `transcribe --language ru
--translate-to en` now works end to end. What it does not do is stream, and the registry
should not say it does.

`RecognitionTier.BATCH` has a member again. It was emptied when ADR 0010 removed Tajik and
kept anyway, on the argument that the distinction would be needed before the language
needing it arrived. Five commits later, it was.

## Why the note matters more than the tier

"Russian is batch" invites the wrong fix. The two failure modes look identical from the
tier alone and have nothing in common:

- **No model exists.** Nothing to do but wait, as with Tajik in ADR 0007.
- **A model exists and cannot be used.** This case. The fix is a licence — an email to the
  republisher, or upstream exporting their model with streaming metadata — and it is
  actionable *today* by someone who is not this project.

So `Language.note` says "no licence-clean streaming model exists", not "no streaming model
exists", and a test asserts the word "licence" survives in it. Losing that distinction
would cost the next person the same afternoon this took.

## Consequences

- `stream --language ru` now refuses with "not a streaming language" and points at
  `transcribe`, rather than the vaguer "no pinned streaming model yet".
- `transcribe` gains `--translate-to`, so the Russian direction works today at batch
  latency. **It is several seconds behind and that is not a conversation** — it is the
  honest maximum for this language until a licence-clean streaming model exists.
- `ru→en` is pinned from OPUS-MT's `opus-2020-02-26` release. Two releases exist for this
  pair; the later scores marginally better on the publisher's own evaluation (BLEU 61.1
  against 60.8, chrF2 0.736 against 0.734) and is the one taken.
- The project now claims **six** streaming languages, not seven. Only English has a pinned
  model even so, and the other five are still unchecked — this ADR does not improve them,
  it corrects one overstatement and leaves the rest visible.

## What would make Russian stream

Any one of:

1. The sherpa-onnx republication declares a licence compatible with Apache-2.0.
2. Alphacephei publishes a streaming export — the same weights with the streaming metadata
   sherpa requires. Their model is already Apache-2.0, so this is a packaging change rather
   than a licensing one.
3. Another publisher releases a licence-clean streaming Russian transducer.

Route 2 is the cheapest and does not depend on this project. It is worth an upstream issue
before anyone here writes conversion code.

## Review trigger

When any of the three above happens, or before another language is promoted to the
streaming tier — this ADR is the record of what "streaming" has to mean before the registry
may claim it.
