# ADR 0009 — Translation: CTranslate2 and OPUS-MT, on finals only

**Status:** Proposed — the engine and the event shape are decided; **the language-coverage question needs a product decision**
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

- Which language pair is implemented first.
- Whether unsupported directions pivot through English, or are refused the way
  `stream --language tg` is refused today.
- Tajik's fate.
- Whether partial translation is ever revisited. The conditions are written above.

## Review trigger

Before the first model is pinned — its licence, and any attribution obligation, checked
individually. Again if measured translation p95 misses 1500 ms on the reference machine, in
which case the per-pair-versus-multilingual choice reopens with evidence behind it rather
than reasoning.
