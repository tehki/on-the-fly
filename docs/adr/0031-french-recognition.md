# ADR 0031 — French, the third language, and why it is the only one available

**Status:** Accepted
**Date:** 2026-09-06
**Deciders:** @tehki
**Risk:** MODERATE — extends what the product may claim, and admits a third model publisher

## Context

[ADR 0007](0007-supported-languages.md) surveyed 134 published `sherpa-onnx` streaming model
repositories and concluded that seven languages could be served at the streaming tier. Two
have been pinned since: English ([ADR 0008](0008-sherpa-onnx-streaming.md)) and Russian
([ADR 0012](0012-russian-streams-after-all.md)). The other five — Spanish, Italian, French,
Portuguese, German — have been named in `domain/languages.py` ever since on the strength of
"a published model exists", which is not the same as one this project can adopt.

[PR #60](https://github.com/tehki/on-the-fly/pull/60) made that gap visible rather than
merely documented: the window had been offering all seven, and now offers what is pinned. So
the question stopped being cosmetic. **Can a third language actually be adopted?**

ADR 0007 also set the bar for answering it: *the tier must be established from evidence
before the code claims it.*

## What was evaluated

Three candidates, in the order they looked promising.

### The kroko family — five languages, one publisher, no licence

ADR 0007 singled this out as the strongest lead: `es`, `fr`, `it`, `de`, `pt` from one
publisher, republished into sherpa-onnx form. The republications carry no licence of their
own. Each says, in full:

> See license at https://huggingface.co/Banafo/Kroko-ASR

That repository declares `license: other`, `license_name: test`, `license_link: LICENSE`.
Its README says community models are CC-BY-SA. **The LICENSE file it points at is empty** —
zero bytes, git object `e69de29bb2d1`, the hash of the empty blob, unchanged since
2025-01-29.

Prose in a README describing a licence is not a grant, and a licence link resolving to
nothing is not one either. ADR 0007's own rule, written about Tajik, applies unchanged:
**no licence is not permission.** Five of the seven languages are blocked on this single
fact, and one commit by that publisher would unblock them.

### `bookbot/sherpa-onnx-zipformer-streaming-robust-es-v0` — Apache-2.0, and not a recogniser this can use

Genuinely Apache-2.0, genuinely small — a 26 MB int8 encoder against English's 70 MB, so
almost certainly the fastest option found. It is also **a phoneme recogniser**. Its
vocabulary is 37 IPA symbols (`a`, `ai`, `au`, `b`, … `t͡ʃ`, `ɲ`, `ɾ`, `ʎ`, `θ`) where the
English pin has 502 word-pieces. It emits `["w", "ɑ", "ʃ", "i", "ɑ"]`, not words.

Nothing downstream can use that. A caption would be unreadable and a translator would be
handed input it was never trained on. Converting phonemes to text is a second model and a
second decision, and neither exists here.

The model card says so plainly. It is recorded because the repository name, licence and size
all look like an adoption candidate right up until the vocabulary is read.

### `shaojieli/sherpa-onnx-streaming-zipformer-fr-2023-04-14` — adopted

Apache-2.0 on both the export and the icefall training repository it names as its source,
trained on Common Voice French. Word-level, 502-token-style vocabulary, three int8 files
and `tokens.txt` at **128.2 MB**.

## Evidence

### Accuracy

The publisher reports, for the exact checkpoint pinned (`epoch-29-avg-9`) and the exact
decoding method this project uses (greedy), on the **full** Common Voice French test set:

| decoding method | chunk | test WER |
| --- | --- | --- |
| **greedy search** | 640 ms | **10.57** |
| modified beam search | 640 ms | 10.19 |
| fast beam search | 640 ms | 10.25 |

That is the number worth having, and it is comfortably inside the 15% this project treats as
the line where output stops being usable ([ADR 0027](0027-reverberation.md)).

Confirmed here on the three test wavs the publisher ships beside the model, decoded through
`scripts/measure_recognition.py` with the int8 export:

| file | words | WER |
| --- | --- | --- |
| `common_voice_fr_19364697` | 13 | 15.4% — `ACHÉMÉNIDE`→`ASHÉMÉNIDE`, `SASSANIDES`→`SASSANDIDES` |
| `common_voice_fr_19738183` | 10 | 20.0% — a dropped `A`, `ÉVOLUÉ`→`ÉVOLUE` |
| `common_voice_fr_27024649` | 12 | 8.3% — `TERRITORIAL`→`DORNATORIAL` |
| **all three** | **35** | **14.3%** |

Thirty-five words is far too small a sample to place against a 15% line, and it is not
offered as one. It establishes something narrower and still necessary: the model loads
through this project's own adapter, decodes French, and produces text a French reader would
recognise — with four of the five errors on proper nouns and rare vocabulary.

### Speed

Measured on the reference machine while it was carrying **a load average of about 6 to 8 on
four cores** from unrelated work. Both models over identical audio, interleaved, six paired
runs:

| | median | range |
| --- | --- | --- |
| English (the shipped pin) | 0.804x | 0.732 – 0.890 |
| **French** | **0.868x** | 0.813 – 1.067 |

**French decodes at about 8% of English's cost more, which is to say: the same.** Both keep
up; both occasionally touch 1.0 when the machine spikes, which is a statement about the
machine. The recorded idle figure for English is 0.399x, so French idle should be near 0.43x
— derived, not measured, because no idle machine was available.

An earlier reading of this was wrong and is recorded because the error is instructive.
Comparing the two models on *their own* test sets gave a French/English ratio between 1.07
and 1.46, and a first single run suggested French could not keep up at all. Different files
of different lengths on a contended machine is not a comparison. Same audio, interleaved,
repeated is.

The full-precision export was measured too, to answer whether int8 quantisation costs
anything: same word error, same speed within noise, for 296 MB instead of 128 MB. int8 is
kept.

### One thing that looked like a defect and was not

`common_voice_fr_19738183` decoded as `...DE L'HISTOIRE RO` against a reference ending
`ROMAINE`. That was the flush defect fixed in
[PR #61](https://github.com/tehki/on-the-fly/pull/61) — a transducer cannot emit a symbol it
has no frames after — and it was found here, on a candidate model, before it was found on
the shipped English one, where it was costing 3.0% word error against 0.0%. Evaluating a
third language paid for itself before the third language shipped.

## Decision

**Pin the French model. Adopt French at the STREAMING tier. Adopt no other language.**

French joins English and Russian as a language this project can recognise. It has **no
translation model pinned in either direction**, so today it produces captions and not
translations — which the target picker already represents honestly as *no translation*
(PR #60), and which is a separate decision with its own artefacts to pin and measure.

The tier is not qualified. `RecognitionTier.STREAMING` means a streaming model exists and is
pinned, and French satisfies both; the speed measurement belongs in
`docs/PERFORMANCE_BUDGET.md` with every other number of its kind, not smuggled into a
taxonomy that is about architecture. `Language.note` stays empty for French, because the
thing it exists to flag — *this language is best-effort and nobody has checked it* — is not
true here.

## What this does not decide

- **It does not adopt French translation.** No OPUS-MT artefact is pinned for `en↔fr`, and
  ADR 0009's rules apply to that decision when it is taken.
- **It does not reject the kroko models.** It rejects a licence that does not exist yet. One
  commit adding a real LICENSE would make five languages evaluable, and this ADR is the
  record of what to re-check.
- **It does not reject phoneme recognition.** It records that a phoneme model cannot be
  dropped into this pipeline as a recogniser, which is a different claim.
- **It says nothing about French through a microphone.** Everything here is recorded speech
  from a public test set. The room, not the model, is what breaks live recognition
  ([ADR 0027](0027-reverberation.md)), and that finding is language-independent.

## Consequences

- Three of seven languages are now real. The README's claim about the other four narrows
  from "a model exists" to "a model exists and here is exactly what is wrong with it".
- A third publisher is admitted, and the first republication of somebody else's training
  run. Both repositories declare Apache-2.0 and the export names its source, which is the
  provenance chain ADR 0009 asks for.
- The download grows by 128.2 MB **only for someone who asks for French**. Models are
  fetched per pin, on request.
- `scripts/measure_recognition.py` now has a second language's worth of use behind it, and
  the next candidate starts from a tool rather than an intuition.

## Review trigger

When `Banafo/Kroko-ASR` publishes a licence, which unblocks five languages at once; when an
`en↔fr` translation artefact is proposed; or when any candidate is found for Italian,
Portuguese or German, which currently have no licence-clean word-level streaming model at
all.
