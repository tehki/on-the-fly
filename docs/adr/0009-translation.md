# ADR 0009 — Translation: CTranslate2 and OPUS-MT, on finals only

**Status:** **Accepted** for English→Russian — see "Decision 5", which records the product
decisions and narrows the model-format question to something a measurement settles
**Revision:** 2026-09-04 — added "Model format, and the pinning problem inherited from ADR 0007", then Decision 5 on acceptance
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — third model trust surface, and it commits the shape of the last stage in the pipeline

## Context

`Translator` has been a port with no implementation since ADR 0002 declared it. The README
still opens by saying so: *"it transcribes, but it does not translate yet."* This is the
decision that closes that gap, and it inherits two things from the ADRs before it — a
latency budget that has just been rewritten, and a licence problem that has bitten twice.

### ADR 0008 changed the arithmetic

The budget's critical path is measured **from the VAD endpoint**, not from the start of the
utterance:

```text
speech ends → recognition → translation → caption rendered
```

Before ADR 0008, recognition owned that path and blew it: ~9 s median against a p95 target
of 1500 ms. Now that the streaming recogniser keeps pace with the audio at 0.399×,
recognition is *already finished* when the endpoint arrives. There is nothing left in the
path.

That has a consequence worth stating plainly, because it sets the whole target for this
work:

> **The endpoint-to-caption budget is now, almost entirely, the translation budget.**
> p50 700 ms, p95 1500 ms, hard limit 2500 ms — for translation alone.

That is a demanding number, but not an unreasonable one for a small sequence-to-sequence
model on a short sentence. It is also, for the first time in this project, a budget that a
single component is wholly responsible for. Every option below is judged against it.

## Decision 1 — translate finals, not partials

The streaming recogniser emits partials that may be revised and finals that will not.
`TranscriptEvent` already asserts which one translation should act on. This ADR is where
that assertion gets its reasoning and, more importantly, its cost.

**Translate finals only.**

| | Finals only | Every partial |
| --- | --- | --- |
| Inferences per utterance | 1 | 16, on the measured English sample |
| Translated text | appears once, correct | rewrites itself as the source is revised |
| Budget | one inference inside 1500 ms | 16 inferences inside the same window |
| `EPHEMERAL` content produced | one translation | 16, of which 15 are discarded |

The compute argument alone would be enough. The retention argument matters more and is less
obvious: **every partial translation is project content that has to be created, held, and
deleted.** Producing fifteen translations nobody will ever read, each one carrying the
ten-second obligation of `docs/RETENTION_POLICY.md`, is work the policy would rather not
exist. Handbook 64S names the inverse failure — retaining data to avoid recomputation — but
the principle runs both ways: do not manufacture sensitive content speculatively.

**What this costs, stated rather than buried.** No translated text appears until the speaker
stops. The *source-language* caption still streams — that is what ADR 0008 bought — so the
screen is not empty while someone talks. But a listener reading the translation waits for
the endpoint plus one inference. For a conversational turn that is the difference between
"live" and "prompt", and it should be described that way in any interface copy rather than
called real-time translation.

**This is reversible and the port does not prevent revisiting it.** A translator that
consumes partials can implement the same interface later. The condition for reopening it is
concrete: measured translation p95 comfortably inside budget *and* a caption design that
handles rewriting without flicker. Neither exists today, and adopting the harder shape
before either one does would be optimising a path nobody has measured.

## Decision 2 — CTranslate2 as the engine

**It is already in the tree.** `faster-whisper` depends on it, so ADR 0005 admitted it
transitively without ever reviewing it as a translation engine.

Verified locally on 2026-09-04 in this project's own environment:

```text
ctranslate2 4.8.2
License: MIT
Requires: numpy, pyyaml          (both already present)
Converters: OpusMTConverter, MarianConverter, TransformersConverter,
            FairseqConverter, OpenNMTPyConverter, OpenNMTTFConverter
ctranslate2.Translator: present
```

### Admission review (Article 12)

| Criterion | Finding |
| --- | --- |
| Licence | **MIT** — verified from installed package metadata |
| Need | Concrete: runs OPUS-MT/Marian and M2M-100 on CPU with int8 quantisation |
| Footprint | **Already installed**, transitively, since ADR 0005. Direct runtime deps `numpy` (already direct) and `pyyaml` |
| Install scripts | None. Wheel |
| Provenance | OpenNMT project, the same publisher whose engine already runs recognition |
| Alternatives | `transformers` + torch — several hundred MB and a much larger trust surface for the same job |

**One honesty point.** CTranslate2 being installed already is *not* the same as it being
admitted. A transitive dependency is something another package chose; depending on it
directly is a choice this project makes and must record. It moves into `requirements.txt`
as a direct pin. That adds no bytes and no new risk — it removes the risk of
`faster-whisper` dropping it and silently breaking translation.

**The one genuinely new dependency is a tokeniser.** `sentencepiece` is confirmed *not*
installed today and is required to tokenise for Marian-family models. It needs its own
admission review before anything is pinned; it is not covered by this one.

## Decision 3 — the models, and the licence survey that had to come first

ADR 0008 learned the hard way that a familiar publisher name is not a licence. So the
licences were checked before the benchmarks this time, not after.

Verified from each model's Hugging Face page on 2026-09-04:

| Model | Licence | Languages | Size | Verdict |
| --- | --- | --- | --- | --- |
| `Helsinki-NLP/opus-mt-en-ru` | **apache-2.0** | one direction | ~75 MB class | **Clean** |
| `Helsinki-NLP/opus-mt-tc-big-en-es` | **cc-by-4.0** | one direction | larger | **Clean, with an attribution obligation** |
| `facebook/m2m100_418M` | **MIT** | 100, any direction | 418 M params | **Clean** |
| `google/madlad400-3b-mt` | **apache-2.0** | "419"/"over 450", not enumerated | **11.8 GB**, 1.65 GB quantised | Clean, **outside the memory budget** |
| `facebook/nllb-200-distilled-600M` | **cc-by-nc-4.0** | 200 | — | **Excluded — non-commercial** |

Three findings worth carrying forward.

**NLLB is exactly the model the README warned about.** It is the strongest obvious candidate
by reputation, and its own card says it "is not released for production deployment". This is
the third time a licence has removed the most convenient option; it is a pattern, not bad
luck.

**The licence varies inside the OPUS-MT family.** `opus-mt-en-ru` is Apache-2.0 while
`opus-mt-tc-big-en-es` is CC-BY-4.0. Both are usable here, but CC-BY-4.0 carries an
attribution requirement, which is a **product** obligation — an attribution notice a user
can reach — and not merely a line in a file. Each pair must be checked individually and its
obligations recorded, exactly as ADR 0008 requires for the recognition models.

**MADLAD-400 is licence-clean and unusable anyway.** At 11.8 GB, or 1.65 GB quantised
against a 1200 MB steady-state target that already holds a recogniser, it does not fit. Its
page does not enumerate its languages, so its coverage claim is **unverified** here.

## Tajik, for the third time

`facebook/m2m100_418M` lists 100 languages. **Tajik is not among them** — verified against
the enumerated list, which runs `... Thai (th), Tagalog (tl), Tswana (tn), Turkish (tr) ...`
with no `tg`.

`Helsinki-NLP/opus-mt-en-tg` could not be confirmed to exist: the URL returns HTTP 401,
which Hugging Face also returns for repositories that are not there. **That is evidence of
absence, not proof of it**, and it needs a direct check against the Hugging Face API before
anyone states it as fact.

So the position is: Tajik has no streaming recognition model (ADR 0007), is transcribed by
Whisper at unverified accuracy (README), and now has **no confirmed licence-clean
translation path either**. MADLAD-400 might cover it and is Apache-2.0, but does not fit in
memory.

The honest options are to serve Tajik at a third, worse tier and say so, or to remove it
from the supported set until something changes. **This ADR does not decide that** — it is a
product question, and it is the one the tagline "speak with anyone worldwide" turns on.

## Decision 4 — per-pair models, not one multilingual model

For eight languages, all-directions coverage is 56 pairs. Per-pair models do not scale to
that; pivoting through English halves the model count but doubles the error and the latency,
inside a budget that has 1500 ms in total.

This is less alarming than it looks, because **a conversation has two languages, not
eight.** A session picks its pair and loads two directions. The N² count is a catalogue
problem, not a runtime one.

**Start with OPUS-MT per-pair models**: they are small, fast on CPU, and their per-pair
licences are checkable. `m2m100_418M` stays the recorded fallback if pair coverage or model
sprawl turns out to be the real problem — it is MIT, covers 100 languages in one model, and
its cost is size and speed. That is a decision to take on measurement, not in advance.

## Model format, and the pinning problem inherited from ADR 0007

**This was missed when this ADR was first drafted.** ADR 0007, investigating Tajik, made an
argument that applies squarely here and was not carried across:

> A pin is a set of SHA-256 digests of files that arrived from a publisher. A locally
> converted artefact has digests that are whatever this machine produced, so a pin over it
> verifies nothing except that the file has not changed since we made it.

Helsinki-NLP publishes OPUS-MT as Marian and transformers checkpoints. **It does not publish
CTranslate2 format.** So "OPUS-MT through CTranslate2" is not a thing that can simply be
pinned and loaded the way `streaming-en` is; something has to convert it first, and whatever
comes out of that conversion is the artefact the digests would cover.

One part of ADR 0007's blocker does *not* apply, and one part applies with full force.

**The dependency blocker is absent here.** `OpusMTConverter` and `MarianConverter` import and
run without `torch` — verified against the installed package on 2026-09-04. Converting from
Helsinki-NLP's *original Marian release* therefore needs no `transformers` and no `torch`.
Only `ct2-transformers-converter`, which reads the Hugging Face checkpoint, drags those in.
ADR 0007's Tajik model is a transformers checkpoint, which is why it hit the dependency wall
and this does not.

**The provenance blocker applies unchanged.** A model this project converts is a model this
project publishes, with the obligations ADR 0007 already spelled out.

### Pre-converted models exist, and they move the problem rather than solve it

Third-party CTranslate2 conversions of OPUS-MT are published on Hugging Face by individuals —
`ordois`, `michaelfeil`, `gaudi`, `manancode` among them. One was checked in detail on
2026-09-04:

| `ordois/opus-mt-en-ru-ctranslate2-int8` | |
| --- | --- |
| Licence | `apache-2.0`, matching upstream |
| Source stated | `Helsinki-NLP/opus-mt-en-ru`, with the conversion command recorded |
| Integrity reference | a SHA-256 of **its own** artefact |

That is a conscientious model card, and it is still not what a pin needs. The digest attests
that the bytes came from `ordois` — it does not establish that the conversion faithfully
represents Helsinki-NLP's weights, and short of repeating the conversion (which needs `torch`)
and finding it byte-reproducible (which nobody has established), there is no way to check.

So pinning a pre-converted model does not remove ADR 0007's problem. It **exchanges it for an
Article 12 publisher-trust problem**, and puts an individual, rather than an institution, in
the supply chain for every sentence this application translates. That may still be the right
trade — it is how most of this ecosystem works — but it is a dependency-admission decision
about a *person*, and it must be recorded as one rather than slipped in as a file path.

### Three routes, none free

1. **Pin a third-party conversion**, and admit that publisher under Article 12 with the same
   scrutiny any dependency gets.
2. **Convert from Helsinki-NLP's original Marian release** — torch-free, unlike the Tajik
   case — and accept becoming the publisher, with provenance recorded and the artefact
   published so the pin covers something a contributor can also obtain.
3. **Find a translation model whose own author publishes CTranslate2 format.** Not surveyed.
   If one exists, it is strictly the cleanest of the three.

**This blocks the first pin. It does not block the engine choice**, which stands on its own
evidence: CTranslate2 is MIT, already present, and runs these models on CPU. Nothing above
argues for a different engine — it argues that "which artefact, from whom" is a separate
decision that this ADR had quietly assumed away.

## Decision 5 — English→Russian first, and what actually gets pinned

Decided 2026-09-04 by @tehki: **English→Russian is the first pair**, and **Tajik is removed
from the project entirely** ([ADR 0010](0010-drop-tajik.md)), which closes the third question
this ADR left open.

### The rule: pin a first-party artefact; never pin a conversion

The three routes above collapse into one principle once it is stated properly.

A pin's job is to attest that particular bytes came from a particular publisher. **A
conversion's output cannot do that job, whoever performs it.** If this project converts, the
digests attest to this machine. If an individual publishes a conversion, they attest to that
individual. Neither is upstream provenance, and no amount of care by the converter changes
what the digest is capable of proving.

So: **pin what the publisher published, and treat the CTranslate2 model as a derived cache.**
Trust flows from a verified input through a build step; the conversion does not need to be
byte-reproducible, because nothing trusts its output digest. That answers ADR 0007's
objection rather than working around it.

This removes the third-party conversion as the primary route — not because the publisher
examined earlier was careless, they were conscientious, but because conscientiousness is not
the property in question.

### Two first-party artefacts exist, and choosing between them is a real trade-off

Both verified 2026-09-04:

| | Marian release (`object.pouta.csc.fi`) | Hugging Face checkpoint |
| --- | --- | --- |
| Publisher | Helsinki-NLP / OPUS-MT, first-party | Helsinki-NLP, first-party |
| en→ru | HTTP 200, 284,142,010 bytes, last modified 2020-02-14 | `Helsinki-NLP/opus-mt-en-ru` |
| ru→en | HTTP 200, 284,237,045 bytes — **and a second release** dated 2020-02-26 | `Helsinki-NLP/opus-mt-ru-en` |
| Ships | `decoder.yml`, `.npz` weights, `vocab.yml`, `source.spm`, `target.spm`, **its own `LICENSE`**, plus `preprocess.sh`, `postprocess.sh`, `source.tcmodel` | transformers checkpoint and tokeniser config |
| Converter | `OpusMTConverter` — **no torch** | `ct2-transformers-converter` — **needs `transformers` and `torch`** |
| Pinning machinery | new: URL plus digest | **existing**: `ModelStore` already pins Hugging Face repos by revision and file digest |

The archive listing was read over an HTTP range request against the central directory, so
the layout above is observed rather than assumed. Note that ru→en publishes **two** releases:
"the" OPUS-MT model for a pair is not a well-defined object, which is an argument for pinning
an exact artefact and a trap for anyone who assumes otherwise.

The trade-off is not provenance — both are first-party. It is this:

- **The Marian release costs preprocessing fidelity.** It ships `preprocess.sh`,
  `postprocess.sh` and a truecasing model, meaning the input pipeline is the publisher's
  shell scripts rather than a bundled tokeniser. Getting that subtly wrong raises no error;
  it quietly produces worse translations. For a tool whose output nobody present can
  spot-check in the target language, silent quality loss is the failure mode that matters
  most — the same argument that removed Tajik.
- **The Hugging Face checkpoint costs a build-time dependency.** Conversion needs
  `transformers` and `torch`. That is the pair ADR 0005 avoided, but here it is needed
  **only at build time and never at runtime**, which is a materially weaker objection than
  ADR 0007's, where the same dependency would have been required to load the model at all.

**Measurement settles this, not argument.** The first implementation converts both, translates
the same sentences, and compares. If the outputs agree, the Marian release wins and the
dependency question evaporates. If they differ, the checkpoint route is correct and torch at
build time is the price of not shipping silently degraded translation.

That comparison is the first task, before any pin is committed. The `LICENSE` inside the
Marian archive is read at the same time and reconciled with the `apache-2.0` the Hugging Face
page declares; a disagreement between them is itself a finding.

## Consequences

- The pipeline gains its last stage, and the interface it is written against does not
  change: `Translator` consumes final `TranscriptEvent` text.
- `ModelStore` takes a third engine's weights. The pinning machinery has now generalised
  twice without modification; if it needs changes for Marian-family files, that is a finding
  worth recording rather than a routine edit.
- `requirements.txt` gains `ctranslate2` as a direct pin, and `sentencepiece` once its own
  admission review is done.
- Translation output is already classified `EPHEMERAL` in the policy, so no new retention
  class or exception is created. **A translation cache is not permitted to change that** —
  caching a translation to avoid recomputing it retains project content past its window,
  which handbook 64F.2 forbids outright.
- The endpoint-to-caption budget becomes measurable end to end for the first time, which is
  what `docs/PERFORMANCE_BUDGET.md` has been waiting for to stop saying PROVISIONAL. It
  should not stop saying it until the reference workload in that document — recorded
  conversational speech, three language pairs, one non-Latin script, 50+ utterances — has
  actually been run.
- CC-BY-4.0 models, if any are pinned, create a user-visible attribution requirement.

## Not decided here

- Whether unsupported directions pivot through English, or are refused the way a language
  with no pinned model is refused today. Moot for English↔Russian, which is direct; it
  returns with the third language.
- Whether partial translation is ever revisited. The conditions are written above.
- **Which first-party artefact is pinned** — the Marian release or the Hugging Face
  checkpoint. Decision 5 narrows this to a measurement and names the experiment.

Settled since drafting: the first pair is English→Russian, Tajik is gone (ADR 0010), and
conversions are never what gets pinned.

## Review trigger

Before the first model is pinned — its licence, any attribution obligation, **and the
format route above, including a publisher-trust review if the artefact comes from a
third-party converter** — checked individually. Again if measured translation p95 misses 1500 ms on the reference machine, in
which case the per-pair-versus-multilingual choice reopens with evidence behind it rather
than reasoning.
