# ADR 0032 — French translation, and two releases of one pair that are not the same kind of artefact

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — extends what the product may claim, and admits two more model artefacts

## Context

[ADR 0031](0031-french-recognition.md) pinned a French recogniser and stopped there: French
produced captions and no translation, because no `en↔fr` artefact had been reviewed. This
closes that, under [ADR 0009](0009-translation.md)'s rules — pin what the publisher
published, never a conversion; follow the licence that travels inside the artefact; decide
decoding width by measurement.

## Choosing a release, twice

Both directions publish two OPUS-MT releases. The publisher re-evaluated every release on a
common set of test suites, so the first round is arithmetic. Averaged over the twenty test
sets both releases of a direction were scored on:

| direction | release | chrF2 mean | wins |
| --- | --- | --- | --- |
| `en-fr` | opus-2019-12-04 | 0.63654 | 4 of 20 |
| `en-fr` | **opus-2020-02-26** | **0.63899** | **16 of 20** |
| `fr-en` | opus-2019-12-05 | 0.63359 | 15 of 20 |
| `fr-en` | opus-2020-02-26 | 0.63247 | 5 of 20 |

Read alone, that says the two directions want releases from different years — `en-fr` the
later, `fr-en` the earlier. The `ru-en` pin was chosen the same way and the later release
won, so this looked like the same decision with a different answer.

**It is not, because the two `fr-en` releases are not the same kind of artefact.** The
publisher's own manifests:

```text
fr-en/opus-2019-12-05.yml   pre-processing: normalization + tokenization + BPE
                            subword-models: source.bpe / target.bpe
fr-en/opus-2020-02-26.yml   pre-processing: normalization + SentencePiece
                            subword-models: source.spm / target.spm
```

`opus_mt.py` tokenises with sentencepiece, as every artefact pinned here does. Loading the
2019 release means admitting a BPE implementation under Article 12 — a new runtime
dependency, on every user's machine, to buy **0.11 chrF2**. Refused, and the later release
is pinned for both directions after all.

The finding that matters beyond this pair: **a score table cannot tell you whether an
artefact is loadable.** Nothing in the twenty rows above hints that one of the four is built
on a different tokeniser.

## Evidence

All four directions this project serves, measured with `scripts/measure_translation.py` on
**1000 sentences** of each pair's own publisher test set, same machine, same session:

| pair | greedy (shipped) | beam 6 | greedy costs | publisher's own output | greedy p50 | beam 6 p50 |
| --- | --- | --- | --- | --- | --- | --- |
| `en→ru` | 64.51 | 64.77 | 0.26 | 65.49 | 330 ms | 817 ms |
| `ru→en` | 71.46 | 72.31 | 0.85 | 72.60 | 319 ms | 488 ms |
| **`en→fr`** | **66.31** | **67.03** | **0.72** | 68.00 | **254 ms** | 840 ms |
| **`fr→en`** | **71.38** | **72.77** | **1.39** | 72.68 | **398 ms** | 820 ms |

French lands between the two Russian directions on quality and is the *faster* of the four
at greedy. Nothing here argues against adopting it.

### The setup was validated before the question was asked

Scoring our output against the publisher's own output measures the pipeline rather than the
model, and beam 6 should reproduce them closely because beam 6 is what they used:

| pair | ours at beam 6 vs their output | ours at greedy vs their output |
| --- | --- | --- |
| `en→ru` | 94.88 | 88.50 |
| `ru→en` | 96.07 | 92.95 |
| `en→fr` | 93.39 | 89.96 |
| `fr→en` | **96.92** | 92.88 |

The chrF2 implementation itself was validated the same way. Scoring the publisher's own
hypotheses against their own references over the **full 5000-sentence** `en-ru` test set
gives **66.95**, where Helsinki-NLP publish 66.9. `sacrebleu` is not a dependency here and
admitting one to compute a number that fits in forty lines is not a trade Article 12 would
approve, so the metric is written out in the script — and then checked against somebody
else's published figure rather than trusted.

### A correction to ADR 0009's reasoning, not its decision

`opus_mt.py` records that greedy decoding costs *"no quality cost that this measurement can
detect"*. That was measured on **one direction, at 300 sentences**, where greedy came out
marginally ahead — 66.62 against beam 6's 66.56 — and it does not generalise. At 1000
sentences every direction pays something, from 0.26 for `en→ru` to **1.39 for `fr→en`**.

**The decision stands and the reason is latency, not parity.** Beam 6 costs 1.5x to 3.3x
more time per sentence, and `docs/PERFORMANCE_BUDGET.md` already records p50
endpoint-to-caption *at* its 700 ms target under load. Adding 400-580 ms to every final
would break the budget outright to buy about one chrF2 point. That is a trade worth stating
honestly rather than one worth hiding behind "no detectable cost".

There is a second reason the sizes differ, and it is worth recording because it invalidates
casual comparisons: **the first 300 sentences of these test sets are not representative.**
On the `en-ru` set, the publisher's own output scores 65.58 over the first 300 and 66.95
over all 5000 — a 1.4 chrF2 gap on identical text with no model involved. Any two numbers
compared here must come from the same slice.

## Licence

Both archives carry `LICENSE` reading *Attribution 4.0 International*, read out of the
downloaded zip rather than off a model page — the rule ADR 0009 set when the Hugging Face
mirror of the `en-ru` model disagreed with the archive it mirrors. CC-BY-4.0 permits
commercial use and obliges attribution, so both attribution lines are printed to the user,
as the existing pairs' are.

## Integrity, and two ways a digest can be honest and wrong

A pin is a SHA-256 of bytes that arrived. Twice in producing these two pins, the bytes that
arrived were not the artefact:

- The first `en-fr` fetch was cut off by a timeout at **191,651,838 bytes** of an advertised
  278,391,154 and produced a perfectly well-formed digest of a truncated file.
- A second `fr-en` fetch, resumed while an earlier attempt was still writing to the same
  path, produced **290,024,502 bytes** — *larger* than the real 285,527,094-byte artefact —
  and again a perfectly well-formed digest.

Neither would have failed any check this project runs, because `TranslationModelStore`
verifies a download against the pin, and the pin is what these would have become. The
existing note in `artifacts.py` — *the digest was taken from a download whose byte count
matched the `content-length` the server advertised* — is the only control that catches this,
and it caught both. It is written down again here because it reads like a formality and is
not one.

## Decision

**Pin `en-fr/opus-2020-02-26` and `fr-en/opus-2020-02-26`. Keep greedy decoding.**

French becomes the third language served end to end, and the first pair added since the
project had two engines, a catalogue and a latency budget to answer to.

## What this does not decide

- **It does not add French to the ONNX engine.** No ONNX export of these artefacts has been
  reviewed, so `--translation-engine onnx --translate-to fr` is refused rather than served
  from the desktop engine. ADR 0018's rule: a caller who asked for the engine that runs on a
  phone must not be told French works there when it has not been checked.
- **It does not revisit the `en↔ru` release choices**, which were made the same way and are
  unaffected.
- **It does not measure spoken French end to end.** The numbers here are text in, text out,
  on the publisher's test sets. What a recogniser's output does to a translator is a
  different measurement.

## Review trigger

If an ONNX export of either artefact is proposed; if the latency budget's translation stage
is revisited, since the greedy decision now has a measured cost rather than none; or if a
third pair is added, at which point the release-selection procedure here is worth extracting
rather than repeating.
