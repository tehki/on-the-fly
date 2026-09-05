# ADR 0018 — A second translation engine, so the product can run on a phone

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — a second inference path, and a model artefact from a converter rather than from the model's authors

## Context

ADR 0017 surveyed what "runs on any hardware" costs and found a single blocker. Recognition
already targets phones: sherpa-onnx ships an official Android `.aar`. **Translation does
not.** CTranslate2 has no Android or iOS build, and the issues asking for one have been open
without resolution. One dependency stood between this pipeline and a phone, and it was not
the expensive stage.

ADR 0017 recorded the way through and deliberately started no port:

> the phase-2 shape is not "find a mobile translation library". It is: **run both models on
> the runtime the recogniser already uses.**

`onnxruntime` is already in this project's dependency tree, arriving under sherpa-onnx, and
it ships official Android (Maven Central) and iOS (CocoaPods) builds at 1.29.0 — the same
version already installed here. This ADR builds that second engine.

## Decision

**Add `OnnxTranslator`, a second implementation of the existing `Translator` port, and keep
CTranslate2 as the default.** Callers choose an engine rather than a class; nothing above
`infrastructure/` changes, and nothing above it learns that a second engine exists.

```text
domain/audio/ports.py     Translator                   unchanged
translation/engines.py    resolve() / open_translator()  the only place the engines differ
translation/opus_mt.py    OpusMtTranslator   CTranslate2  default, faster
translation/onnx_translator.py  OnnxTranslator  ONNX Runtime  runs where the other cannot
```

The port ADR 0002 designed for this was never load-bearing until now. It held: the pipeline,
the CLI, the desktop worker and the retention path are untouched by a whole second inference
engine, and the diff that added one is confined to `infrastructure/translation/` plus a flag.

## What had to be written rather than imported

Sequence-to-sequence generation. `transformers` provides it and requires `torch`, which is
the multi-gigabyte dependency ADR 0005 declined and the single thing that cannot go on a
phone — importing it to reach a phone would defeat the exercise. So the greedy decoding loop
is about sixty lines in `onnx_translator.py`: encode once, step the decoder one token at a
time, carry the key/value cache.

Two details in it are not obvious, and both are asserted by tests rather than left to a
reader:

- **Two decoder graphs, not the merged one.** The export ships a merged decoder with a
  `use_cache_branch` switch. Its no-cache path fails on a zero-length encoder cache — a
  `Reshape` error on `encoder_attn` with dimension zero. The separate `decoder_model` and
  `decoder_with_past_model` pair has no such ambiguity.
  > **Corrected 2026-09-05.** This is wrong. The no-cache path works. The `Reshape` error
  > comes from the *cached* branch returning placeholder `present.*.encoder.*` outputs of
  > shape `(0, 8, 1, 64)`, which a loop that copies them back then feeds in one step later.
  > The merged graph runs all 300 test sentences once it is left alone — and is twice as
  > slow, which is the actual reason it is not used. See the second amendment below.
- **The encoder cache is computed once and carried unchanged.** Cross-attention keys and
  values depend only on the source sentence, so the with-past graph does not return them.
  A loop that expects them back either crashes or silently re-attends to nothing, and
  recomputing them per token would roughly double the work.

The loop is simple only because the decoding strategy is greedy, which ADR 0014 and the
sixth and seventh measurements established costs no measurable quality. Beam search on this
runtime would be considerably more code.

## The artefact, and an admission this project has not had to make before

ADR 0009 set the rule: **pin what the publisher published, never a conversion.** The
CTranslate2 route obeys it by pinning Helsinki-NLP's own `.zip` and treating the converted
directory as a derived cache that no digest attests to.

**That rule cannot be followed here, and saying so plainly matters more than appearing to
follow it.** Helsinki-NLP publish Marian weights; they do not publish an ONNX export. ADR
0009 anticipated exactly this and named the alternative:

> Pin a third-party conversion, and admit that publisher under Article 12 with the same
> scrutiny any dependency gets.

The route not taken was exporting it here, which requires `optimum`, `transformers` and
`torch` — and would make this project the publisher of an artefact whose digest would attest
to the machine that produced it and nothing else, which is precisely ADR 0007's objection.

### Admission review — `onnx-community` (Article 12)

| Criterion | Finding |
| --- | --- |
| What it is | The Hugging Face organisation publishing ONNX exports for `transformers.js` |
| Declared licence | **cc-by-4.0** — the licence inside Helsinki-NLP's own archive |
| Base model | `Helsinki-NLP/opus-mt-en-ru`, declared in the repository's metadata |
| Pinned to | Revision `c6967b32`, plus a SHA-256 for each of the seven files loaded |
| Executes anything at load | No. `onnxruntime` reads a graph; the repository ships no code |
| Alternative considered | `Xenova/opus-mt-en-ru` — **declares no licence at all**, so not taken |

The licence line is the strongest signal available and is worth stating precisely. This
export declares **cc-by-4.0**, agreeing with the `LICENSE` inside the publisher's `.zip` and
disagreeing with the `apache-2.0` that the Hugging Face mirror of the same model claims
(ADR 0009 recorded that conflict). A converter who tightened the declared licence to match
the upstream artefact rather than loosening it to match the convenient mirror is a converter
paying attention.

**What the review cannot establish is that the weights are Helsinki-NLP's.** No published
digest connects an ONNX export to a Marian archive, and the two formats are not comparable
byte for byte. The check actually available is behavioural, and it was run — see below. It
is evidence of the same weights, not proof, and it is recorded as the former.

## The measurement

Both engines were loaded through the application's own factory, so what was measured is what
the application runs. 300 sentences from Helsinki-NLP's own `en-ru` test set, scored with
chrF2 against their human references — the same set, metric and method as the sixth and
seventh measurements, so the numbers are comparable to what is already in
`docs/PERFORMANCE_BUDGET.md`.

### Quality

| | chrF2 vs human references |
| --- | --- |
| CTranslate2, int8 (the default) | **66.62** |
| ONNX Runtime, int8 | **66.33** |
| Published score for this model | 66.9 |

**0.29 chrF2 apart, and the CTranslate2 figure reproduces the sixth measurement's 66.62
exactly** — same corpus, same metric, a different day and a rebuilt model directory. That
reproduction is what makes the comparison worth reading at all.

**The quantisation is not where the gap is.** The full-precision graphs score **66.34** —
the same as the quantised 66.33 — for 653 MB against 421 MB. So int8 costs nothing here, and
the 0.29 against CTranslate2 is the export rather than the compression. Worth the one
download it took to check, because "int8 is close enough" would otherwise have gone into
this ADR as an assumption.

### Latency

300 sentences per cell, greedy and single-threaded on both sides, load created deliberately
for the loaded columns:

| | CTranslate2 idle | ONNX idle | CTranslate2 loaded | **ONNX loaded** |
| --- | --- | --- | --- | --- |
| p50 | 144 ms | 383 ms | 277 ms | **674 ms** |
| p95 | 235 ms | 647 ms | 572 ms | **1259 ms** |
| Model load | 0.99 s | **9.18 s** | — | — |

**2.7x slower idle, 2.4x under load**, and the ratio holding across conditions is the part
worth trusting — both arms ran in one process, so contention hits them equally.

That is why CTranslate2 stays the default and why the desktop window does not offer a
picker. Under load the ONNX stage alone is 674 ms against a 700 ms endpoint-to-caption
target, which leaves nothing for recognition: on this hardware the second engine would miss
the budget. It exists for the hardware where the choice is not between two engines but
between one engine and none.

### Agreement between the engines

| | |
| --- | --- |
| Identical output | **239 of 300 sentences (79.7%)** |
| chrF2, ONNX against CTranslate2 output | **94.1** |

That is the behavioural evidence that the weights are the same model. Four fifths of
sentences match character for character, and the disagreements are of a kind that
quantisation produces rather than a different model: `чёрное`/`черное`, one word's gender
ending, an alternative that means the same thing.

### And through the pipeline, not just the unit

The ninth measurement caught a beam size that was correct in the class, correct in the test,
and wrong in the product, because nothing exercised the path the CLI takes. So both engines
were run end to end on the same audio through `stream`, not only in a harness:

```text
AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE THE SQUALID QUARTER...

ctranslate2   После наступления ночи желтые лампы загорались здесь, а там - ...
onnx          После раннего наступления ночи желтые лампы загорались здесь, а там - ...
```

One word apart on an eighteen-word sentence; 412 ms against 927 ms for it; retention clean
on both runs; real-time factor 0.393x against 0.442x, both keeping up.

## Consequences

- **The blocker ADR 0017 identified is gone.** Both stages of the pipeline now have an
  implementation on a runtime with official Android and iOS builds. That is not the same as
  running on a phone, and the next section says so.
- **`--translation-engine {ctranslate2,onnx}`** on `transcribe` and `stream`. The default is
  CTranslate2 and the desktop window uses the default without offering a picker: a desktop
  has no reason to run the slower engine, and a setting nobody on a desktop should change is
  a setting that does not belong in the window.
- **Refusing rather than falling back.** A pair the requested engine cannot serve raises. If
  asking for ONNX quietly returned the CTranslate2 translator, the product would be telling
  someone it runs on their hardware when it does not — the failure mode this whole ADR
  exists to remove.
- **`onnxruntime` is now a declared dependency** rather than a transitive one. It adds no
  bytes; it was already installed under sherpa-onnx. This is the third promotion of that
  kind in `requirements.txt`, for the same reason each time: a transitive dependency is
  another package's choice.
- **421 MB of ONNX graphs against 84 MB for the CTranslate2 model**, two thirds of it the
  decoder weights carried twice — once in `decoder_model`, once in `decoder_with_past_model`.
  The merged graph would save 183 MB of that, **works**, and costs twice the latency; the
  duplication is bought deliberately rather than forced. On a phone this is a product
  decision rather than a footnote.
- **A second artefact to maintain.** `en→ru` only. `ru→en` has no ONNX pin, so asking for it
  on this engine is refused rather than served from a repository nobody reviewed.

## What this does not establish

- **Nothing here ran on a phone.** No Android or iOS device was available. What is
  established is that the translation stage now runs on the runtime that has mobile builds,
  which is a necessary condition and not a sufficient one. Capture and the interface are
  still desktop-only (ADR 0017), so a phone build remains unbuilt work rather than a
  packaging exercise.
- ~~**`ru→en` on ONNX is unmeasured**, because it is unpinned.~~ **Closed 2026-09-04** — see
  the amendment below. Both pinned pairs now run on both engines.
- **The quality comparison is short single-reference sentences.** chrF cannot separate
  "different but equally correct" from "worse", and 61 sentences differ. The same caveat
  ADR 0009 records for beam size applies unchanged here.

## Amendment, 2026-09-04 — the other direction, and the defect it exposed

This ADR shipped with `en→ru` only, which the seventh measurement's own lesson says is half
a product: *a decision validated on one direction is a decision validated on half the
product*. `ru→en` is now pinned to `onnx-community/opus-mt-ru-en` @ `92ef0d55`, same
publisher, same cc-by-4.0, same admission review.

**Which Marian release that export descends from was checked rather than assumed.** The
Hugging Face checkpoint it converts names `opus-2020-02-26.zip` as its original weights —
the same release `artifacts.py` pins for CTranslate2, and the later of the two this pair
publishes. Both engines therefore run the same model, which is the thing that would
otherwise silently explain any difference between them.

A free cross-check came with it: this export's `source.spm` digest equals the `en→ru`
export's `target.spm`, and their `vocab.json` files are byte-identical. OPUS-MT trains a
pair on one joint sentencepiece vocabulary, so that is what two directions of one model
family should look like — and it is asserted by a test rather than noticed once.

### The defect the second direction exposed

Measuring `ru→en` turned up a bug in the loop this ADR describes. One sentence in 300 came
back as `<pad>` repeated to the token budget — **9.7 seconds of work for output that was
pure padding** — and others carried a stray `<pad>` mid-sentence.

OPUS-MT uses **one id (62517) for both padding and the decoder start token**, and the
publisher's `generation_config.json` says so:

```json
"bad_words_ids": [[62517]]
```

`transformers` applies that constraint as a matter of course. A generation loop written out
by hand does not — which is the specific cost of the trade this ADR made to keep torch off a
phone, and it is worth naming as such rather than filing as an ordinary bug. The loop now
masks those logits before the argmax, `generation_config.json` is part of the pin (an
unverified constraint is not a constraint), and a multi-token entry is **refused** rather
than skipped, because this greedy loop cannot enforce a forbidden *sequence* and pretending
otherwise would be the same mistake one level down.

```text
Сошлитесь на мою предыдущую статью.
  before   <pad><pad><pad>... (256 tokens)          9726 ms
  after    Please refer to my previous article.      288 ms
```

### Both pairs, after the fix

300 sentences per direction, chrF2 against the publishers' human references:

| | CTranslate2 | ONNX | gap |
| --- | --- | --- | --- |
| `en→ru` | **66.62** | 66.33 | 0.29 |
| `ru→en` | **73.17** | 72.59 | 0.58 |

Both CTranslate2 figures reproduce this repository's earlier measurements exactly — 66.62
from the sixth, 73.17 from the seventh — which is what makes the ONNX column readable.

The fix moved `ru→en` from 72.25 to **72.59** and lifted engine agreement from 253 to
**255 of 300 identical** (chrF2 95.0 between them). It changed `en→ru` by nothing at all:
that direction never hit the pad token in 300 sentences, so a measurement of it alone would
have found none of this. **The bug was reachable from the beginning and only the second
direction surfaced it**, which is the argument for covering both.

### End to end, both directions

```text
ru→en, the Russian sample published with the pinned recogniser
  ctranslate2   Rodon of the poppist counted every new creep and long ago determined
  onnx          Rodon poppist counted every new piece of depth and long ago determined
```

Retention clean on both, 0.252x real time on ONNX. Recognition drops the proper noun in
both cases; that is the recogniser, not the translator.

## Second amendment, 2026-09-05 — the merged decoder is not broken, it is slow

The first version of this ADR said the merged decoder "fails on a zero-length encoder
cache". **That was wrong**, and it was wrong in the way this repository keeps catching: a
single failure, diagnosed once, generalised into a property of the artefact. The claim went
into an ADR, a module docstring, a source comment and the README before anyone checked it.

The merged graph decodes all 300 test sentences. What actually happens is one step further
on: **on the cached branch it returns placeholder `present.*.encoder.*` outputs of shape
`(0, 8, 1, 64)`.** A loop that copies every `present.*` back into its cache — the obvious
thing to write — feeds that placeholder in on the next step, and *that* raises the `Reshape`
error on `encoder_attn`. The traceback points at the encoder cache, so the first reading was
"the no-cache path cannot handle an empty encoder cache". The real rule is the one the
two-graph implementation already follows for its own reasons: **the encoder half of the
cache is written once and never overwritten.**

### What it costs, now that it runs

300 sentences, `en→ru`, greedy, single-threaded, one process:

| | two graphs (shipped) | merged |
| --- | --- | --- |
| Decoder graphs on disk | 370 MB | **187 MB** |
| Total artefact | 421 MB | **238 MB** |
| chrF2 vs human references | **66.33** | 65.82 |
| p50 | **323 ms** | 633 ms |
| p95 | **540 ms** | 1102 ms |
| Identical output | — | 263 of 300 |

**Twice the latency to save 183 MB, and 0.51 chrF2 worse.** The quality difference is real
but small and comes from the merged export being quantised as its own graph — same weights,
different rounding — which also explains the 37 sentences that differ.

**Not adopted.** Latency is this engine's binding constraint: it is already two to three
times CTranslate2, and the twelfth measurement showed the stage alone consuming the
endpoint-to-caption budget under load. Doubling it to halve a download is the wrong side of
that trade while the target is still a desktop.

**Worth revisiting on a phone**, where 421 MB is a different kind of problem than it is on a
laptop, and where the execution provider is not the one measured here. Recorded with the
numbers so that revisit starts from evidence rather than from this paragraph.

The mechanism behind the 2x was not established — the merged graph binds
`encoder_hidden_states` on every step and evaluates an `If` node the split pair does not
have, and either could dominate. What is established is the cost as the interface presents
it, which is what the decision needed.

## Review trigger

- If CTranslate2 gains mobile support, the reason to maintain two translator implementations
  disappears — worth watching for rather than assuming (ADR 0017 already records this).
- Before a mobile build starts, because capture and interface are the remaining ports.
- **`ModelPin` and `ModelStore` live under `infrastructure/asr/` and are not speech-specific.**
  This ADR reuses them from the translation package rather than duplicating them, and moving
  them to `infrastructure/` proper is the correct end state. Deliberately not bundled here: it
  touches eleven files and would make the diff that adds an engine mostly a rename.
