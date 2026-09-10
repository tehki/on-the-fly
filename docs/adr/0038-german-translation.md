# ADR 0038 — German translates, without being recognised here

**Status:** Accepted
**Date:** 2026-09-09
**Deciders:** @tehki
**Risk:** MODERATE — admits four more model artefacts and adds a language to every picker

## Context

Seven languages are supported and four of them — Spanish, Italian, Portuguese, German — could
be transcribed and **not translated into anything at all**. `--translate-to de` was refused,
and so was German as a source. For a product whose first line is *live speech translation*,
that is four sevenths of the language list doing the half that is not the point.

Those four are the batch tier ([ADR 0034](0034-tiers-describe-this-project.md)): no streaming
model is pinned for them, because the one family that publishes them ships an **empty**
`LICENSE` file ([ADR 0007](0007-supported-languages.md)). That is a recognition problem, and
it has been treated as though it settled translation too. It does not. A translation target
needs a translation model and nothing else — `catalogue.py` has said so in a docstring since
the pickers were derived — so *somebody speaking English can be read in German* without any
streaming model existing for German at all.

German is taken first because it is the one of the four that can be taken without a new
argument. Same publisher, same object store, same tokeniser, same licence, both engines.

## Spanish cannot be taken, and it is worth writing down why

`es-en` and `en-es` publish exactly one release each, `opus-2019-12-04`, and the publisher's
own manifest for both says:

```yaml
pre-processing: normalization + tokenization + BPE
subword-models:
   - source: source.bpe
```

[ADR 0032](0032-french-translation.md) refused a release for exactly this: every artefact here
tokenises with sentencepiece, and loading a BPE model means admitting a BPE implementation on
every user's machine under Article 12.

> **The last sentence of this section said Spanish "needs a release somebody else has not
> published". Re-checked 2026-09-10: that is wrong, and the door is shut by something else.**
>
> The release exists — in the other bucket. `Tatoeba-MT-models/spa-eng/opus-2021-02-19.zip`
> and its `eng-spa` twin are **sentencepiece** (`spm32k`), and `onnx-community` publishes
> `opus-mt-es-en` and `opus-mt-en-es` declaring `cc-by-4.0`, naming `Helsinki-NLP/opus-mt-es-en`
> as their base and shipping `source.spm`. On the ONNX side Spanish is admissible today.
>
> What fails is [ADR 0033](0033-french-on-onnx.md)'s chain, at its last link. That checkpoint
> names its original weights as `Tatoeba-MT-models/spa-eng/opus-2020-08-18.zip`, and **that
> file is gone** — HTTP 404, `NoSuchKey`. The archive the export derives from is no longer
> published, so the CTranslate2 side would have to pin a *different* release than the one the
> ONNX side carries. That puts two different models behind one pair, which is exactly what the
> chain check exists to prevent: ADR 0033 wrote down the consequence, that a difference
> "would then have been attributed to the runtime".
>
> So Spanish is still refused, for a better reason and a more hopeful one. It needs one dead
> link to come back, or one export of a release that still exists — not a model nobody has
> trained.
>
> `scripts/survey_translation_models.py` now performs this check for every unserved pair, so
> the next person does not have to remember to.

**German publishes three releases per direction and two of them are the same trap.**
`opus-2019-12-04` and `opus-2019-12-18` are BPE; `opus-2020-02-26` is sentencepiece. The pin is
an exact URL and a test reads the year out of it, so the rejection is visible in the pin rather
than only here.

## The chain, followed in both directions before anything was fetched

Same two declared links [ADR 0033](0033-french-on-onnx.md) insisted on, checked 2026-09-09:

| Link | `de-en` | `en-de` |
| --- | --- | --- |
| `onnx-community` declares its base model | `Helsinki-NLP/opus-mt-de-en` | `Helsinki-NLP/opus-mt-en-de` |
| That checkpoint names its original weights | `de-en/opus-2020-02-26.zip` | `en-de/opus-2020-02-26.zip` |
| Which is the archive `artifacts.py` pins | yes | yes |
| Declared licence on the export | `cc-by-4.0` | `cc-by-4.0` |
| `LICENSE` inside the Marian archive | CC-BY-4.0 | CC-BY-4.0 |

The Article 12 admission of `onnx-community` ([ADR 0018](0018-onnx-translation.md)) applies
unchanged: same organisation, same declared licence, no executable code path at load.

One thing the digests show rather than assume: this pair's two exports **share a vocabulary**.
`de-en`'s `source.spm` is `en-de`'s `target.spm` byte for byte, and `vocab.json` is the same
file in both. That is what a jointly trained 32k sentencepiece pair looks like from outside,
and it is a small independent check that these two exports come from one training run.

## Measurement

1000 sentences of each direction's own publisher test set, this project's chrF2 against the
human references, on a machine at load average 8 to 12 on four cores. The publisher's own
output is the third line of that file, so their score is measured on identical text:

| pair | theirs (beam 6) | ours (greedy) | greedy costs | p50 |
| --- | --- | --- | --- | --- |
| `de→en` | 71.61 | 70.82 | 0.79 | 270 ms |
| `en→de` | 66.40 | 65.48 | 0.92 | 287 ms |

That is the same shape the sixteenth measurement found for four other directions: greedy costs
between 0.26 and 1.39 chrF2 and saves 400–580 ms, and the trade still goes the same way. Both
p50s sit with the four pairs already shipping, `de→en` being the second fastest of six.

**Both engines, as ADR 0033 requires.** Same sentences, same metric:

| pair | CTranslate2 | ONNX | difference |
| --- | --- | --- | --- |
| `de→en` | 70.82 | 70.47 | −0.35 |
| `en→de` | 65.48 | 65.45 | −0.03 |

`en→de` agrees to within a rounding error, as the French pair did. `de→en` is 0.35 apart, the
largest gap measured between these engines — larger than Russian's 0.29 and an order above
French. It is the same *kind* of difference ADR 0018 traced to the export rather than the
quantisation, and 0.35 chrF2 is well inside what greedy decoding already costs. It is recorded
rather than explained: no digest connects an ONNX graph to a Marian archive, and behavioural
agreement is the only check available.

## What German gets for free, and what it gives

[ADR 0037](0037-french-and-russian-through-english.md) landed a week before this, so both
German pairs are legs the moment they are pinned. Twelve ordered pairs among four languages now
resolve; six are pinned and six are bridged.

**`de↔ru` has no other route.** Helsinki-NLP publishes no bilingual German-Russian model at
all — the object store has no `de-ru` or `ru-de` prefix, and no such Hugging Face repository
exists. For that pair the bridge is not the cheaper option, it is the only one this publisher
offers.

`de↔fr` does have direct models, and they were measured the same way ADR 0037 measured the
Russian pair — the publisher's own output against the same references:

| direction | direct model | via English | difference |
| --- | --- | --- | --- |
| `de→fr` | **67.00** | 62.48 | −4.52 |
| `fr→de` | **68.07** | 64.41 | −3.66 |

**The bridge loses this one, in both directions,** where ADR 0037 found it winning `fr→ru` by
5.44. The two results say the same thing: a bridge is dominated by its second leg and lands
near it whatever the pair, so it beats a *weak* direct model and loses to a strong one. The
direct `fr→ru` scores 57.27; the direct `de→fr` and `fr→de` score 67.00 and 68.07, which is
above what any bridge through English reaches.

Pinning them anyway was considered and is not available on the terms this project holds
itself to. `onnx-community` **publishes no `de-fr` export at all**, and its `fr-de` export
**declares no licence** — the same two dead ends ADR 0037 hit for `fr-ru`. Serving these pairs
directly would mean CTranslate2 only, spending the invariant ADR 0033 bought, or admitting an
unlicensed artefact. So the bridge stays, 3.66 to 4.52 chrF2 behind a model this project
cannot ship on both engines, and the numbers to beat are written here for whenever that
changes.

## Decision

**Pin `opus-2020-02-26` for `de-en` and `en-de`, on both engines, and let the bridge do the
rest.** German becomes a translation target for every streaming source and a translation
source for files.

The tier table is not touched. German stays `BATCH`, because the tier describes *recognition*
and nothing about recognition changed. What changes is that the two questions are now visibly
independent: the language pickers offer German as a target and not as a source, and that is the
honest description of what this project can do with it.

## What this does not do

- **It does not make German stream.** The licence problem ADR 0007 recorded is unchanged, and
  no measurement here touches it.
- **It does not add Spanish, Italian or Portuguese.** Spanish is blocked — on a broken link
  rather than on the BPE release this ADR first blamed, see the note above. Italian and
  Portuguese have not been checked and this ADR claims nothing about them.

  > Italian was taken in one direction by [ADR 0039](0039-italian-one-way.md); Portuguese is
  > blocked twice over, and the same ADR records both reasons.
- **It does not make `de→en` a live path.** German audio goes through the batch tier — an
  utterance at a time, after the speaker stops ([ADR 0035](0035-the-batch-tier-measured.md)).
  The live direction is *into* German.
- **It does not re-open greedy decoding.** The 0.79 and 0.92 above are consistent with the four
  pairs already measured and change nothing about that trade.

## Review trigger

When a sentencepiece release of `es-en`/`en-es` is published, or when a licensed streaming
model for any batch-tier language appears — either would change which half of this decision
matters.
