# ADR 0008 — sherpa-onnx as the streaming engine

**Status:** Accepted for English; the other six languages need their own licence checks
**Date:** 2026-09-02
**Deciders:** @tehki
**Risk:** MODERATE — second recognition engine, second model trust surface

## Context

ADR 0006 defined the streaming port and deferred the engine. ADR 0007 fixed the language
set and confirmed sherpa-onnx publishes streaming models for seven of the eight. This adopts
it, with English as the reference implementation.

## Admission review (Article 12)

| Criterion | Finding |
| --- | --- |
| Licence | **Apache-2.0** |
| Footprint | **49 MB installed, one transitive package** (`sherpa-onnx-core`). faster-whisper is 265 MB across 20 |
| Runtime | ONNX Runtime, already present transitively. **No torch** |
| Install scripts | None. Wheel |
| Phase 2 | Targets mobile, which is where ADR 0002 goes. faster-whisper does not |

## The measurement

The reason for all of this. Real English speech — the 6.62 s sample published alongside the
model at the same pinned revision — fed 20 ms at a time:

| | sherpa-onnx streaming | faster-whisper batch |
| --- | --- | --- |
| Real-time factor | **0.399×** | several times over 1.0× |
| First text appears | **after 1.10 s of audio** | only after the utterance ends |
| Per-utterance cost | keeps up with the audio | ~9 s median, flat in length |
| Events | 16 partials, then 1 final | one result, at the end |

The transcript was correct: *"AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND
THERE THE SQUALID QUARTER OF THE BROTHEL"*, with partials growing sensibly — `AFTER` →
`AFTER EARLY` → `AFTER EARLY NIGHTFA`.

**This is the first correct speech recognition this project has produced**, and the first
configuration that keeps up with live speech.

### One number that got worse

Model load took **13.46 s**, against a 3 s startup target. Some of that is a contended
machine and a cold first load of the ONNX session, and the same machine produced a 2× spread
on the Whisper benchmark — but it is over budget as measured and is recorded as such rather
than explained away. It is a one-off cost per session, not per utterance.

## The segmenter is not in this path

A transducer decides where an utterance ends from the audio itself, so
`UtteranceSegmenter` does not run ahead of it. Two endpointers disagreeing would produce cuts
nobody could explain, and the model's own endpointing is what makes it streaming rather than
merely fast.

That has a consequence worth stating: the pre-roll ring that bounded retention on the batch
path is not present here. The bound is instead the recogniser's own internal buffers, which
are fixed by the model's chunk and left-context configuration — the pinned variant is
`chunk-16-left-64`, chosen because the smaller left context is the lower-latency one.

## A licence finding that applies to the remaining six languages

Checking model licences individually, as the Tajik episode taught:

| Model | Licence |
| --- | --- |
| `sherpa-onnx-streaming-zipformer-en-2023-06-26` | **Apache-2.0** — pinned |
| `sherpa-onnx-nemo-streaming-fast-conformer-transducer-en-480ms` | **none declared** |
| `sherpa-onnx-streaming-zipformer-small-ru-vosk-2025-08-16` | **none declared** |

Being published under a sherpa-onnx name does not make a model licensed. Two of the three
checked declare nothing at all, and no licence is not permission.

**So the six remaining languages are not yet cleared.** ADR 0007 counted models; it did not
check their licences. Each of Russian, Spanish, Italian, French, Portuguese and German needs
the same check before it is pinned, and it is possible some have no licence-clean option —
which would be the Tajik problem again, in a smaller way.

## Consequences

- English streams, correctly, faster than real time.
- Two engines now coexist: sherpa-onnx for streaming, faster-whisper for batch and for the
  languages streaming cannot serve. Both sit behind domain ports; neither is visible above
  `infrastructure/asr/`.
- `ModelStore` needed no changes to trust a second engine's weights. The pinning machinery
  generalised, which is what it was built for.
- A second runtime dependency, though a small one.

## Review trigger

Before pinning each remaining language — its licence must be checked first. Also if the
startup cost stays above budget on a quiet machine, in which case model loading needs
attention rather than the excuse it currently has.
