# ADR 0039 — Italian, in one direction, on purpose

**Status:** Accepted
**Date:** 2026-09-09
**Deciders:** @tehki
**Risk:** MODERATE — adds a language that works in one direction only, which is a claim a user
can misread

## Context

[ADR 0038](0038-german-translation.md) established that recognition and translation are
separate questions and took German on the strength of it. It left three batch-tier languages
untranslated and said explicitly that Italian and Portuguese had not been checked.

This checks them. The result is asymmetric in a way worth recording, because the asymmetry is
the decision rather than an accident of effort.

## Portuguese cannot be taken

| | `pt→en` | `en→pt` |
| --- | --- | --- |
| OPUS-MT-models release | `opus-2019-12-05` — BPE | `opus-2019-12-04` — BPE |
| ONNX export by `onnx-community` | none | none |

One release each, both `normalization + tokenization + BPE`, which is what
[ADR 0032](0032-french-translation.md) refused for French and ADR 0038 refused for Spanish.

The **Tatoeba-MT-models** bucket does publish sentencepiece Portuguese releases
(`opus-2021-02-18`, `opus-2021-02-23`, `spm32k`), from the same publisher under the same
licence, and that is a real route for CTranslate2. It fails the second test:
`onnx-community` has exported exactly two `opus-mt-tc-big-*` models and both are Turkish, and
`Helsinki-NLP/opus-mt-tc-big-pt-en` does not exist as a checkpoint at all. That road ends at a
desktop-only pair, which is the invariant [ADR 0033](0033-french-on-onnx.md) bought and ADR
0037 and ADR 0038 both declined to spend.

## Italian passes in one direction and fails in the other

| | `it→en` | `en→it` |
| --- | --- | --- |
| Marian release | `opus-2019-12-18` — **SentencePiece** | `opus-2019-12-04` — **SentencePiece** |
| Checkpoint names that archive | yes | yes |
| ONNX export | `onnx-community/opus-mt-it-en`, **`cc-by-4.0`**, base model declared | `onnx-community/opus-mt-en-it` — no README, no card metadata, **no licence, no base model** |
| Archive members | as every artefact pinned here | `...transformer.model1.npz...`, a different shape |

**The `en→it` refusal is the part worth writing down.** That export sits inside the
organisation [ADR 0018](0018-onnx-translation.md) admitted under Article 12, and it is still
refused. The admission was never a judgement about a name: it rested on the repository
declaring `cc-by-4.0` — agreeing with the `LICENSE` inside Helsinki-NLP's own archive — and
naming the checkpoint it converted. An artefact in the same organisation that declares neither
carries none of that evidence, and `Xenova` was refused for exactly this. Article 12 admits
publishers on evidence about artefacts; treating admission as a namespace would make the
review a formality performed once.

`it-en` publishes two releases and the earlier one is BPE, which is the **third** pair where
that is true. The pin is an exact URL and a test reads the date out of it.

## Measurement

1000 sentences of the publisher's own `it-en` test set, whose third line is the publisher's
own output, so their score is measured on identical text:

| | theirs (beam 6) | ours (greedy) | greedy costs | p50 |
| --- | --- | --- | --- | --- |
| `it→en` | 80.44 | **79.85** | 0.59 | **177 ms** |

**Both engines**, same sentences: CTranslate2 79.85 against ONNX **79.66**, −0.19 — inside the
range every pair but `de→en` has managed.

**79.85 is the highest chrF2 in this project and the number to be most careful with.** It is
not evidence that Italian translates better than German: the sets are different sentences and
scores across pairs are not comparable. What is comparable is the 0.59 against the publisher's
own output on identical text, which is the smallest greedy cost measured here and says the
setup is right. 177 ms is a like-for-like number, and it is the fastest pair this project
serves.

Bridged from Italian, against the direct models' own output on their own test sets:

| direction | direct model | via English | difference |
| --- | --- | --- | --- |
| `it→fr` | **79.07** | 69.17 | −9.90 |
| `it→de` | **66.60** | 64.28 | −2.32 |

Six bridge readings now exist and they line up. What a bridge *scores* moves with the pair;
what it **loses** moves with how strong the direct model is: −2.32 against a 66.60 model,
−9.90 against a 79.07 one, and +5.44 against the 57.27 `fr→ru` model in ADR 0037. `it→fr` is
the largest loss measured and also the strongest direct model measured — those are the same
fact. A bridge is a route to a pair that has none, and it is not an argument against pinning a
direct model wherever one can be pinned on both engines.

`it→ru` has no direct model published at all, as `de→ru` has none — the second pair where the
bridge is not the cheaper route but the only one.

## Decision

**Pin `it→en` on both engines. Do not pin `en→it`. Do not take Portuguese.**

Italian becomes a source that is not a target, which is the mirror of German — a target that
is not a source. Neither is tidy and both are true, and the pickers already derive from the
registries, so each language appears exactly where it can be served.

Four new pairs: `it→en` pinned, and `it→fr`, `it→de`, `it→ru` bridged.

## What this does not do

- **It does not let anyone be answered in Italian.** Somebody speaking Italian can be
  understood; the reply cannot be written back to them. That is a real limit and the README
  says it in the same breath as the addition.
- **It does not make Italian live.** Like German it is the batch tier — an utterance at a time,
  from a file ([ADR 0035](0035-the-batch-tier-measured.md)).
- **It does not lower the bar for `en→it`.** One commit adding a licence and a base model to
  that export would make it admissible under the existing review, and nothing else has to
  change.
- **It does not admit the Tatoeba-MT-models bucket.** Portuguese would need that store *and* an
  ONNX export that does not exist; taking the store alone would buy a desktop-only pair.

## Review trigger

When `onnx-community/opus-mt-en-it` declares a licence and a base model, or when any
`opus-mt-tc-big-*` Portuguese export appears — either turns half a language into a whole one.
