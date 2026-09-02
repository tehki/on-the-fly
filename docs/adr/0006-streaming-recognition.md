# ADR 0006 — Streaming recognition, and the language trade-off it forces

**Status:** Accepted for the architecture; **the engine choice is deferred and needs a product decision**
**Date:** 2026-09-02
**Deciders:** @tehki
**Risk:** MODERATE — commits the shape of the pipeline's most expensive stage

## Context

ADR 0005 delivered recognition and measured it missing the latency budget. The decision
taken from that measurement was to pursue a **streaming-capable** recogniser rather than
accept batch latency.

Before acting on it, the measurement was repeated properly, and the earlier number was
wrong enough to correct.

### Correction to the ADR 0005 measurement

ADR 0005 recorded "~4400 ms per utterance". That came from a handful of samples. A clean
run — one model instance held for the whole benchmark, seven samples per configuration —
gives:

| Audio length | min | median | max |
| --- | --- | --- | --- |
| 2 s | 6.71 s | 8.87 s | 14.78 s |
| 10 s | 8.38 s | 8.94 s | 13.27 s |
| 25 s | 8.73 s | 9.44 s | 11.16 s |

Two things follow, one reassuring and one not.

**The flat-cost hypothesis is confirmed.** Two seconds and twenty-five seconds of audio cost
the same. Whisper pads every input to a 30-second window, so a short conversational turn is
the *worst* case for it. That was the important claim and it holds.

**The absolute numbers are unreliable on this machine**, and the earlier 4.4 s was a
favourable sample rather than a typical one. The spread — 6.7 s to 14.8 s for identical
input — is too wide for a machine that also ran a browser and a test suite. `cpu_threads=4`
made things worse than the default, not better, which is another sign of contention rather
than a tuning finding.

Recorded because handbook 64N is explicit that a single favourable run is not evidence, and
the honest position is: **the direction is certain, the magnitude is not.** Whichever number
is right, it is several times over a 1500 ms budget, and the conclusion does not depend on
which.

## Decision

**Adopt the streaming shape now, defer the engine.**

`StreamingRecognizer` is defined in `domain/audio/streaming.py`: it consumes frames and
emits `TranscriptEvent`s, partial or final. It depends on no engine.

`BatchStreamingRecognizer` implements it today by driving the existing segmenter and Whisper
recogniser, emitting **finals only**. It does not make Whisper fast — nothing can, the cost
is inside the model — but it means the pipeline, the tests and every caller are already
written against the interface a real streaming engine needs. Swapping one in becomes an
adapter change rather than an application change.

## The trade-off that has to be a product decision

Streaming buys latency and costs language coverage. That collision is not a detail.

| | Whisper (batch) | Streaming transducers |
| --- | --- | --- |
| Latency | whole utterance, then ~9 s | tokens as you speak |
| Languages | **99, one model** | typically one language or one bilingual pair per model |
| Model size | 78 MB (tiny) upward | tens of MB |

The repository's own description is *"speak without bounds with anyone worldwide."* A
streaming-only pipeline does not deliver that today, because streaming models are trained
per language. Three ways to resolve it:

1. **Hybrid.** Streaming for a chosen set of languages; Whisper for the long tail, with its
   latency and an interface that says so. Best product, most work.
2. **Streaming only.** Fast and honest, with a narrower promise than the tagline. The
   tagline would have to change.
3. **Batch only.** Keeps 99 languages, and the tool is not live. ADR 0005 already shows
   what that feels like.

**Recommended: (1).** The port defined here is what makes it possible — a session picks its
recogniser, and the application above does not care which it got.

**Not decided here:** which streaming engine, which models, which languages. That is the
next decision and it needs the product answer above first.

## Leading engine candidate

**sherpa-onnx** (k2-fsa), if the hybrid path is taken.

Verified by fetching the wheel: **Apache-2.0, 2.3 MB, exactly one dependency**
(`sherpa-onnx-core`), against faster-whisper's 265 MB across 20 packages. It runs on ONNX
Runtime, which this project already has transitively, and needs no torch.

Not verified here, and taken from the project's own documentation: its catalogue of
streaming Zipformer and Paraformer transducers, their language coverage, and their accuracy.
Those claims need testing on real speech before anything is adopted — which is exactly what
ADR 0005's model-pinning machinery is for, and it applies unchanged to a second engine.

A point in its favour beyond latency: it targets mobile, which is where ADR 0002 says this
project goes in phase 2. faster-whisper does not.

## Consequences

- The application is written against a streaming interface from now on, before any engine
  that streams exists. That is the point: the interface is the commitment.
- `BatchStreamingRecognizer` emits no partials and says so through `emits_partials`, so a
  caption renderer can skip its rewrite handling rather than guessing.
- An empty transcript produces no event at all. Emitting an empty final would make a caption
  renderer clear the screen for nothing.
- Latency is measured per utterance and carried on the event, so the budget can be judged
  from real traffic rather than a benchmark.
- **The budget is still missed.** This ADR changes the shape, not the speed.

## Review trigger

When the product question above is answered. Also if a multilingual streaming model appears
that removes the trade-off entirely — that would make this ADR's central tension obsolete,
and it is the outcome worth watching for.
