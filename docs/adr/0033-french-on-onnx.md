# ADR 0033 — French on the engine a phone can run

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — admits two more model artefacts from a third-party converter

## Context

[ADR 0018](0018-onnx-translation.md) added a second translation engine because CTranslate2
has no Android or iOS build, and [ADR 0017](0017-any-hardware.md) requires that the product
be able to run where it is going. [ADR 0032](0032-french-translation.md) then added French on
CTranslate2 only, and said plainly what that left: `--translation-engine onnx --translate-to
fr` was refused rather than served, because refusing is better than telling a caller their
pair runs on a phone when nobody has checked.

This checks it. `onnx-community` publishes exports of both French directions, from the same
organisation ADR 0018 admitted under Article 12 for Russian.

## Which model, not just which licence

The Article 12 review ADR 0018 performed for `onnx-community` applies unchanged — same
publisher, same declared `cc-by-4.0`, same absence of any executable code path, same
behavioural rather than cryptographic link to Helsinki-NLP's weights. What is worth
recording is the check that mattered *more* here than it did for Russian.

`fr-en` publishes two releases and ADR 0032 refused one of them: the 2019 release is a **BPE**
model where this project tokenises with sentencepiece. An ONNX export of that vintage would
have loaded, produced plausible French, and quietly been a different model on one engine than
on the other — a difference that would then have been attributed to the runtime.

So the release was established before either export was fetched, and the chain has two
declared links, both checkable:

```text
artifacts.py pins            OPUS-MT-models/{en-fr,fr-en}/opus-2020-02-26.zip
Helsinki-NLP/opus-mt-*       README names that exact archive as its original weights
onnx-community/opus-mt-*     base_model names that Hugging Face checkpoint
```

Run across all four pairs this project serves, every mirror names the archive `artifacts.py`
pins — `opus-2020-02-11.zip` for `en-ru` and `opus-2020-02-26.zip` for the other three. The
`ru-en` entry in `onnx_artifacts.py` already recorded this check for one pair; it is now done
for all of them.

## Two cheap structural checks

**The joint vocabulary.** OPUS-MT trains a pair on one sentencepiece vocabulary, so each
direction's `source.spm` must be the other's `target.spm`, and `vocab.json` must be identical
across the two repositories. It is, and only `config.json` differs. Two unrelated exports
that happened to share a naming convention would not do that. Asserted in tests rather than
observed once.

**Identical sizes are not identical weights.** Every encoder in both repositories is
50,065,734 bytes and every decoder 178,814,001 — one architecture and one vocabulary size,
which is expected and which also perfectly disguises a repository published twice by mistake.
The digests differ. That is checked, and asserted.

Every file was also verified against the size the API advertises before being digested, which
is the control ADR 0032 added after two downloads produced well-formed digests of bytes that
were not the artefact.

## Evidence

1000 sentences of each pair's own publisher test set, both engines, same machine, same
session, on a machine carrying a load average of 6 to 8 on four cores:

| pair | CTranslate2 | ONNX | difference | CT2 p50 | ONNX p50 |
| --- | --- | --- | --- | --- | --- |
| `en→fr` | 66.31 | 66.26 | **−0.05** | 254 ms | 418 ms |
| `fr→en` | 71.38 | **71.45** | **+0.07** | 398 ms | 411 ms |

**The two engines agree to within a rounding error, in both directions**, and `fr→en` comes
out marginally ahead on ONNX — which is noise, and is the point: there is no measurable
quality difference to explain.

That is a better result than Russian, where ADR 0018 measured a 0.29 chrF2 gap and
established it was the export rather than the quantisation, by paying 232 MB for the
full-precision graphs and getting the same score. No such gap exists here, so that question
does not arise.

It is also the strongest available evidence that these exports carry Helsinki-NLP's weights.
No digest connects an ONNX graph to a Marian archive and the two formats are not comparable
byte for byte, so the check is behavioural — and two independently converted artefacts
scoring within 0.07 chrF2 of the archive-derived conversion, in both directions, is what
"the same model" looks like from outside. Evidence, not proof, and recorded as the former.

**Speed is not a single number and is not quoted as one.** ONNX measured 1.0x to 1.7x the
CTranslate2 latency across these runs — 1.65x for `en→fr` and 1.03x for `fr→en`. ADR 0018 saw
1.6x to 3.0x for Russian and declined to give a decimal for the same reason: the machine
these were taken on is carrying other work, and a ratio between two contended measurements
does not support the precision. What the numbers do support is the shape of the decision,
which is unchanged — CTranslate2 is at least as fast everywhere measured, so it stays the
default, and the portable engine is for hardware where the choice is between one engine and
none.

## Decision

**Pin both exports. French runs on both engines, and CTranslate2 stays the default.**

Nothing about the engine choice changes: ADR 0018's reasoning — the faster engine on the
desktop, the portable one where the choice is between one engine and none — applies to
French exactly as it applies to Russian.

## What this does not decide

- **It does not claim French runs on a phone.** It claims French runs on the runtime that
  has official Android and iOS builds. Nothing in this project has been run on a phone, and
  ADR 0017's survey is still a survey.
- **It does not revisit the merged decoder.** ADR 0018 measured it as twice as slow and 0.51
  chrF2 worse and bought the 183 MB of duplication deliberately; French changes none of the
  inputs to that.
- **It does not add a fifth pair.** The registries are now equal, and a new test fails if a
  pair is ever pinned on one engine and forgotten on the other.

## Review trigger

When anything is actually run on a phone, which is what would turn ADR 0017's survey into a
measurement; or if `onnx-community` republishes either revision, since the pin is by commit.
