# ADR 0035 — The batch tier, measured, and what it does not carry

**Status:** Accepted. **Qualifies [ADR 0034](0034-tiers-describe-this-project.md)**, whose tier
assignment stands and whose silence about accuracy does not.
**Date:** 2026-09-07
**Deciders:** @tehki
**Risk:** LOW — a measurement and a documentation change; no code path changes

## Context

[ADR 0034](0034-tiers-describe-this-project.md) moved Spanish, Italian, Portuguese and German
from `STREAMING` to `BATCH`, on the finding that no licence-clean streaming model could be
adopted for them and that they *are* served, an utterance at a time, by the Whisper model
`transcribe` already pins. It was explicit about what it did not claim:

> **It does not claim these four languages work well.** Nobody here has measured Whisper
> `tiny` on any of them … `BATCH` means *recognised an utterance at a time, several seconds
> behind*, which is what has been demonstrated — not a quality claim.

`models.py` has said since ADR 0005 that `tiny` is "honestly not accurate enough to ship a
translator on". Also never measured.

Both are now measurable, because this project acquired the tooling to do it: two published
test sets with human references, and `scripts/measure_recognition.py`. The script grew a
`--whisper` mode so the batch and streaming recognisers are scored by the same word error
implementation on the same files — a comparison is worth nothing if the two halves are
measured differently.

## Evidence

Whisper `tiny` against the pinned streaming models, same audio, same references, same
normalisation:

| test set | Whisper `tiny` | the streaming pin | files |
| --- | --- | --- | --- |
| English (`csukuangfj` test wavs) | 6.1% | **0.0%** | 2, 66 words |
| French (`shaojieli` test wavs) | **77.1%** | **14.3%** | 3, 35 words |

### English, read closely, is better than 6.1%

Three of the four errors in 66 words are orthography rather than recognition:

| reference | Whisper | what it is |
| --- | --- | --- |
| `DISHONOURED` | `dishonored` | a spelling convention |
| `FOR EVER` | `forever` | a compound split, counted twice |
| `PARENT` | `parrot` | a genuine misrecognition |

So the honest English figure is nearer **1.5%**, and the metric — which deliberately does not
normalise orthography — is an upper bound. `models.py`'s claim is **not supported for clean
read English**: on this sample `tiny` is fine.

### French is the finding

77.1%, and not the kind of error a reader can see past. The third clip in full:

```text
reference   SON ACTIONNAIRE MAJORITAIRE EST LE CONSEIL TERRITORIAL DE SAINT PIERRE ET MIQUELON
tiny        Sur une action est armée, je vais être à l'alcool, c'est à l'intérieur du sampeur
            et mes culons.
```

That is not a transcript with mistakes in it. It is a different sentence. The pinned French
streaming model returns the reference almost exactly on the same file.

It is also **0.994x real time overall and 1.812x on that clip** — so on this machine the batch
path is not reliably faster than the audio either, which is the one thing a batch recogniser
is usually good for.

## Decision

**Keep the tier. Stop letting it imply a quality it has not got.**

`BATCH` is a statement about latency and remains correct: those four languages are recognised
an utterance at a time rather than live. What ADR 0034 left implicit — that "served" means
"served usefully" — is now known to be wrong for at least one high-resource language on clean
audio, so the documents say so and the command line stops recommending `transcribe` as though
it were an equivalent.

## What this does and does not establish

- **It does not measure Spanish, Italian, Portuguese or German.** No test set with human
  references was found for them in a licence-clean sherpa-onnx repository, which is the same
  wall ADR 0031 hit. French is offered as the nearest evidence available: a high-resource
  European language, read speech, clean recordings, published by the model's own author —
  close to the easiest case there is. The four are unlikely to do better, and that is an
  inference, labelled as one.
- **It does not condemn Whisper.** It measures `tiny`, the 78 MB model this project pins to
  prove the pipeline on a CPU. A larger Whisper would very likely score far better and would
  be a different pin, a different download and a different latency decision.
- **It does not remove the batch path.** It is the only thing those four languages have, and
  something a user can inspect beats nothing. What changes is that the application no longer
  offers it as though it were equivalent to a pinned streaming model.
- **Sample sizes are small** — 66 and 35 words. Large enough to see a 77% failure; not large
  enough to rank two models that are close.

## Consequences

- The four `BATCH` languages carry a caveat that mentions accuracy, not only the absence of a
  streaming model, so `has_caveat` guards something worth guarding.
- `models.py`'s remark about `tiny` is corrected: it is wrong about clean English and right
  about the case that matters, which is any other language.
- `scripts/measure_recognition.py` measures both engines, so this comparison can be repeated
  rather than believed.

## Review trigger

If a larger Whisper model is pinned; if a licence-clean test set with references appears for
any of the four languages, which would replace the inference above with a measurement; or if
a streaming model is adopted for one of them, which retires the question.
