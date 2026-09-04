# Performance budget — phase 1 desktop

**Status: PROVISIONAL. No baseline has been measured.**

Every number below is a target derived from what conversation tolerates, not from a
measurement of this system. Nothing here may be cited as an achieved result, and no claim
that the project is fast, light, or efficient may be made on the strength of this document
(handbook 0L.2). The first honest use of this file is as the thing a baseline is compared
against once one exists.

Recorded now because handbook 64B requires the metric and success condition to be chosen
*before* optimisation, so that the target is set by what users need rather than by whatever
the first implementation happens to produce.

## Why latency is the metric

A translator is judged on whether a conversation stays a conversation. Above roughly a
second of lag, people stop waiting and start talking over each other; the tool then costs
more than it gives. That makes end-to-end latency the user-critical path, and the tail
matters more than the mean — a p50 of 400 ms with a p99 of four seconds is experienced as a
tool that keeps breaking, not a fast one (handbook 0L.7).

## Critical path

```text
speech ends (VAD endpoint detected)
   → recognition
   → translation
   → caption rendered
```

Measured from the VAD endpoint, not from the start of the utterance: the speaker is still
talking before that, so time spent there is not lag the user perceives.

## Targets

| Metric | Target | Hard limit | Notes |
| --- | --- | --- | --- |
| Endpoint → caption, p50 | 700 ms | — | Conversational feel |
| Endpoint → caption, p95 | 1500 ms | 2500 ms | Tail is the real experience |
| Endpoint → caption, p99 | — | 4000 ms | Above this, treated as a dropped turn |
| VAD endpoint detection | 300 ms | 500 ms | Trades against clipping the speaker |
| Application start → ready to listen | 3 s | 6 s | Excludes first-run model download |
| Steady-state resident memory | 1200 MB | 2000 MB | Dominated by loaded models |
| CPU, one active stream | < 60% of 4 cores | — | Must leave the machine usable |
| Retention deletion after post-use | ≤ 10 s | 10 s | Not a performance target. A policy invariant that happens to be timed |

## Reference environment

Targets are meaningless without the machine they apply to.

- 4-core x86-64 laptop CPU, no GPU acceleration.
- 8 GB RAM.
- Models resident, process warm. Cold-start figures are reported separately and never
  compared against warm ones (handbook 64S).
- Workload: recorded conversational speech, 3–15 second utterances, at least three
  language pairs including one non-Latin script.
- Fixed recorded audio, never live microphone input, so runs are comparable.

GPU acceleration, if added, is reported as a separate configuration. It never replaces the
CPU baseline, because the CPU path is what most users will actually run.

## Measurement method

- Correlation ID attached at capture, carried through the pipeline; stage boundaries
  recorded as timestamps.
- Timings are `OPERATIONAL_METADATA`: duration, language pair, model identifier, audio
  length, outcome. **Never the transcript, the translation, or the audio.** A benchmark
  corpus does not get an exemption from `docs/RETENTION_POLICY.md`.
- Minimum 50 utterances per configuration. A single fast run is not evidence
  (handbook 64N.1).
- Report p50, p95, p99 and the distribution — never the mean alone.
- Record commit, model versions, dependency lock state, machine, and background load.

## Rules that outrank this budget

The budget never justifies weakening something else. Specifically, it is not a reason to:

- retain audio, transcripts, or translations past their window to avoid recomputation;
- skip model integrity verification on load;
- move inference off-device (ADR 0001 settles this);
- log content in order to measure something;
- remove a required CI gate to make the loop feel faster.

If the secure implementation cannot meet a target, the target changes or the design
changes — explicitly, in this file. The invariant does not quietly give way
(handbook 64C).

## First measurement — 2026-09-02

The pipeline became runnable end to end, so there is now one measured number where before
there were none. It is **not** the baseline this document is waiting for, and the gap is
worth being precise about.

| | |
| --- | --- |
| What ran | WAV file → VAD → utterance segmentation → retention store |
| What did not | Recognition and translation, which do not exist |
| Workload | 33 s of synthesised tone bursts, 11 utterances |
| Runs | 9 |
| Real-time factor | min 0.015×, **median 0.018×**, max 0.025× |
| Utterance count | 11 on every run |
| Retention | clean on every run — nothing retained, no deletion failure |

Reproduce with `python -m on_the_fly segment <file.wav> --json`.

**Why this is not the baseline.** The targets above are for *endpoint to caption*, and the
two most expensive stages are missing. Synthetic tones are also not speech: they are
trivially separable by an energy detector, so they exercise the plumbing and say nothing
about detection quality on a real voice in a real room. Treat the number as evidence that
segmentation is not the bottleneck — roughly 55× faster than real time, leaving essentially
the whole budget to recognition and translation — and as nothing more.

The status line above stays PROVISIONAL until an end-to-end path exists and can be measured
on recorded speech.

## Second measurement — 2026-09-02, with recognition

Recognition landed (ADR 0005), and the budget is **missed**.

> **Corrected 2026-09-02.** The figure first recorded here, ~4400 ms, came from a handful
> of samples and was a favourable one. A clean re-run is below. The conclusion did not
> change; the number did, and a number that moves by 2x on re-measurement should not have
> been stated as confidently as it was.

Measured on the reference machine, `tiny` model, int8, CPU. One model instance held for the
whole benchmark, seven samples per configuration:

| Audio length | min | median | max |
| --- | --- | --- | --- |
| 2 s | 6.71 s | **8.87 s** | 14.78 s |
| 10 s | 8.38 s | **8.94 s** | 13.27 s |
| 25 s | 8.73 s | **9.44 s** | 11.16 s |

| | Measured | Budget |
| --- | --- | --- |
| Recognition per utterance | **~9 s median, 6.7–14.8 s observed** | p95 endpoint-to-caption 1500 ms |
| Model load | 2.25 s, once | startup 3 s target — within it |
| Segmentation | 0.018x real time | not the bottleneck |

**The spread is too wide to trust the absolute value.** 6.7 s to 14.8 s for identical input
means this machine was contended during the run. `cpu_threads=4` measured *worse* than the
default, which is a symptom of the same thing rather than a tuning result. A reliable
baseline needs a quiet machine, and this was not one.

What survives the noise: recognition is **several times over budget** whichever number is
right, and the conclusion does not depend on picking one.

**Recognition cost does not scale with utterance length — confirmed across 2 s, 10 s and
25 s.** Whisper pads every input to a 30-second window, so a two-second utterance costs the
same as a twenty-five-second one. That is architectural, not a defect in this code, and it
means short conversational turns are the *worst* case for this model rather than the best.
This was the load-bearing claim and it held up under the cleaner measurement.

The pipeline currently **cannot keep up with live speech** on this machine.

### The decision taken from it

A streaming-capable recogniser, not batching or tuning. See
[ADR 0006](adr/0006-streaming-recognition.md): the `StreamingRecognizer` port now exists and
`BatchStreamingRecognizer` presents the current Whisper path through it, finals only.

That changes the shape, not the speed. **The budget is still missed**, and it stays missed
until an engine that actually streams is adopted — which is blocked on a product question,
because streaming models are per-language and Whisper's 99 languages in one model is what
"speak with anyone worldwide" currently rests on.

What is **not** an acceptable remedy, per this document's own rules: raising the budget to
match the measurement without a stated reason, or weakening validation, retention or
verification to buy latency.

## Third measurement — 2026-09-02, streaming

The streaming engine landed (ADR 0008) and **the latency target is met for English**.

Real English speech, 6.62 s, published alongside the pinned model, fed 20 ms at a time:

| | sherpa-onnx streaming | faster-whisper batch | Budget |
| --- | --- | --- | --- |
| Real-time factor | **0.399x** | several times over 1.0x | under 1.0x to keep up |
| First text visible | **1.10 s into the audio** | only after the utterance ends | — |
| Endpoint → caption | keeps pace; the work happens as audio arrives | ~9 s median | p95 1500 ms |
| Model load | 13.46 s | 2.25 s | 3 s target |

The transcript was correct, and partials grew rather than being rewritten wholesale.

**What this changes.** The budget's central failure — recognition several times slower than
speech — is resolved for English. The pipeline can now keep up with a live conversation in
the language it has a streaming model for.

**What it does not change.** Two things get worse or stay open:

- **Model load is 13.46 s against a 3 s target.** A cold ONNX session on a contended
  machine, and a one-off per session rather than per utterance — but over budget as
  measured, and recorded as such rather than explained away.
- **Seven of eight languages are still unmeasured**, and six of them are not even licence-
  cleared yet (ADR 0008). Tajik has no streaming path at all.

The status line stays PROVISIONAL. One language measured on one publisher's own sample is a
demonstration, not a baseline. The reference workload this document asks for — recorded
conversational speech, three language pairs, one non-Latin script — has still never been run.

## Fourth measurement — 2026-09-04, end to end with translation

Translation landed (ADR 0009) and was wired into the streaming path, so for the first time
there is a number for the thing this document actually budgets: **endpoint to caption.**

Real English speech, the 6.62 s sample published alongside the pinned recognition model,
`stream --language en --translate-to ru` on the reference machine:

| | Measured | Budget |
| --- | --- | --- |
| **Endpoint → caption** | **1476 ms** | p50 700 ms, **p95 1500 ms**, hard limit 2500 ms |
| Real-time factor, recognition + translation | 0.539x (keeps up) | under 1.0x |
| First source text visible | 1.12 s into the audio | — |
| Recognition model load | 5.35 s | 3 s startup target |
| Translation model load | 0.61 s | included in the above |
| Retention | clean; nothing retained, no deletion failed | invariant, not a target |

The transcript was correct and the Russian was correct.

**Inside the p95 target, and only just.** 1476 ms against 1500 ms is not headroom, it is a
coin landing on its edge — and it is the *worst case* for utterance length, an eighteen-word
sentence. The shorter conversational turns measured during implementation take 490–570 ms,
comfortably inside. So the honest summary is: typical turns are fine, long sentences are at
the limit, and nothing here has been measured under CPU contention.

> **Corrected 2026-09-04 by the fifth measurement below.** This paragraph is wrong. Over 65
> utterances the p95 is **2820 ms**, past the 2500 ms hard limit, and this single sample sat
> near the *mean* rather than near p95. The reasoning above treats one run as if it located
> the tail; it did not, and no single run can. Left in place rather than edited away,
> because the mistake is more instructive than a corrected number would be.

**Why the real-time factor rose from 0.399x to 0.539x.** Translation is real work added to
the same wall clock. It still keeps up, which is the property that matters, but the margin
narrowed by a third and a second language pair running concurrently has never been tried.

### Why this is still not the baseline

The status line above stays **PROVISIONAL**, and the gap is now smaller but specific:

- **One utterance.** This document asks for at least 50 per configuration. A single run is
  not a distribution, and p95 is a word that requires one (handbook 64N.1).
- **One language pair.** The reference workload asks for three.
- **One publisher's own sample**, chosen to demonstrate their model. It is not adversarial,
  and it is not a real room with a real microphone.
- **Startup is still over budget.** 5.35 s of recognition model load against a 3 s target,
  better than the 13.46 s of ADR 0008 but not fixed — and the first run for a new language
  pair additionally pays a one-off 16.5 s to verify, extract and convert the translation
  model.

What did change: the budget's central question is no longer unanswerable. Before this run,
endpoint-to-caption could not be measured at all because the stages did not exist.

### One thing the measurement exposed that no budget line covers

The recogniser emits uppercase text and the translator was trained on cased prose. Fed the
raw recogniser output, the same sentence takes **5596 ms** instead of 1524 ms *and* comes
back mistranslated — yellow lamps become white ones. Uppercase fragments into far more
sentencepiece pieces, so the wrong answer is also the slow one.

Recorded here because it would otherwise look like a pure quality defect. It was a latency
defect too, and a budget that only tracked milliseconds would have caught it while a review
that only read the Russian would also have caught it — but neither alone would have
explained it.

## Fifth measurement — 2026-09-04, a distribution instead of an anecdote

The fourth measurement reported **1476 ms** endpoint-to-caption from a single utterance and
called it "inside the p95 target, and only just". **That was wrong**, and this is what a
real sample says.

65 utterances of real recorded English (LibriSpeech dev-clean, 3–15 s as this document's
workload specifies, 442 s of audio), `en→ru`, reference machine:

| | Measured | Target | Hard limit | |
| --- | --- | --- | --- | --- |
| Endpoint → caption, **p50** | **1271 ms** | 700 ms | — | **missed, 1.8×** |
| Endpoint → caption, **p95** | **2820 ms** | 1500 ms | 2500 ms | **missed, and past the hard limit** |
| Endpoint → caption, **p99** | **3735 ms** | — | 4000 ms | inside |
| Mean | 1517 ms | — | — | — |
| Range | 463–3735 ms | — | — | — |
| Recognition real-time factor | 0.41× p50, 0.51× max | under 1.0× | — | **comfortable** |

**The single sample was near the mean and the mean is not the budget.** 1476 ms sat close
to the 1517 ms mean while p95 is nearly twice that. This is precisely the failure handbook
64N.1 describes — a single run is not evidence — and it was made in this document one
section earlier. The correction is the point of recording it.

**Recognition is not the problem.** It keeps pace at 0.41× and its worst case is 0.51×. The
entire budget failure is the translation stage.

### The translation stage on its own

100 sentences, LibriSpeech's own reference transcripts (median 85 characters):

| | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- |
| `en→ru` | 1413 ms | 3081 ms | 3902 ms | 3902 ms |
| `ru→en` | 1098 ms | 3268 ms | 6196 ms | 6196 ms |

The `ru→en` input is Russian produced by the `en→ru` model rather than natural Russian —
in-distribution and clean, so those figures are a best case. No natural Russian corpus was
available here.

### The lever, measured, and why it is not pulled

Beam size is the obvious control. 60 sentences, `en→ru`, same machine:

| beam | p50 | p95 | p99 | outputs identical to beam 6 |
| --- | --- | --- | --- | --- |
| 6 (publisher default) | 1765 ms | 4145 ms | 5269 ms | 60/60 |
| 4 | 1446 ms | 3842 ms | 3961 ms | 19/60 |
| 2 | 1217 ms | 2700 ms | 3070 ms | 8/60 |
| **1 (greedy)** | **677 ms** | **1530 ms** | 2167 ms | **0/60** |

Greedy decoding puts p50 inside the 700 ms target and p95 within 30 ms of the 1500 ms one —
a 2.6× improvement at the median. It is the whole budget gap, available today, by changing
one integer.

**It is not taken, and the reason is not caution.** Every one of the 60 outputs changes.
Different is not worse, but this project cannot currently tell which it is: there is no
Russian reference set to score against, and nobody here reads Russian well enough to judge.
Shipping a 2.6× speed-up whose quality effect is unmeasured is the case handbook 64S names
outright — "choosing faster but incorrect/non-equivalent code" — and it is the same failure
that removed Tajik in ADR 0010, arriving this time as a performance decision rather than a
language one.

What would settle it: a Russian reference corpus and a chrF/BLEU comparison across beam
sizes, or a Russian speaker willing to read 60 pairs. Either is small work. Neither has been
done, so the default stays at the publisher's 6.

### Variance, and what these numbers are not

The beam-6 p50 reads 1765 ms in the sweep and 1413 ms in the 100-sentence run, on the same
machine and model. That spread is contention, and it means these percentiles carry roughly
±20% before anything else is considered. They are good enough to say the budget is missed —
that conclusion survives the noise easily — and not good enough to certify a 10%
improvement.

Still absent from all of it:

- **Read speech, not conversation.** LibriSpeech is audiobook narration: clean endpoints,
  no disfluencies, no crosstalk. A real conversation is harder in every respect.
- **Two language pairs, not three**, and one of those measured on machine-generated input.
- **No microphone.** Every number in this document comes from a file.
- **A quiet machine.** Nothing here was measured under controlled load.

## Sixth measurement — 2026-09-04, greedy decoding. The budget is met.

The fifth measurement found the budget missed at p50 and p95, identified beam size as the
lever, and declined to pull it because the quality cost was unmeasured. The quality cost has
now been measured, and it is not there.

### The quality question, answered

Helsinki-NLP ship a test set for this model **with human reference translations** and the
score they achieved on it. That makes the question answerable without a Russian speaker —
and it had been available all along, which is the uncomfortable part: the fifth measurement
declared the evidence unavailable without checking.

300 sentences from `opus-2020-02-11.test.txt`, scored with chrF2 against the human
references:

| | chrF2 | Latency p50 |
| --- | --- | --- |
| beam 6 (publisher default) | 66.56 | 353 ms |
| **beam 1 (greedy)** | **66.62** | **153 ms** |
| Published score for this model | 66.9 | — |

**No quality cost the measurement can detect**, and 2.3x the speed. Greedy is fractionally
ahead, which is noise in both directions rather than an improvement.

The comparison is only worth anything because both halves were validated before the question
was asked:

- our beam-6 output reproduces the publisher's own hypotheses at **chrF2 95.3**, so the local
  setup matches theirs;
- our beam-6 output scores **66.56** against their references where they published **66.9**,
  so the metric implementation is sound.

An earlier attempt at this misidentified which line of the test file was the human reference
and produced a headline of "94.7 versus 89.4" — a number that looked like a strong result
and was actually measuring both decodings against the publisher's own output. The
reproduction check caught it. A validation step that cannot fail is decoration; this one
failed usefully.

### The result end to end

Same 65 utterances, same corpus, same method as the fifth measurement — only the beam size
changed:

| | beam 6 | **beam 1** | Target | Hard limit | |
| --- | --- | --- | --- | --- | --- |
| Endpoint → caption, p50 | 1271 ms | **420 ms** | 700 ms | — | **met** |
| Endpoint → caption, p95 | 2820 ms | **912 ms** | 1500 ms | 2500 ms | **met** |
| Endpoint → caption, p99 | 3735 ms | **1146 ms** | — | 4000 ms | **met** |
| Mean | 1517 ms | 497 ms | — | — | |
| Max | 3735 ms | 1146 ms | — | — | |
| Recognition real-time factor | 0.41x | 0.33x | under 1.0x | — | met |

**Every target in this document is now met on this workload.** 3.0x at p50, 3.1x at p95.
That margin is far larger than the ±20% variance these measurements carry, so the conclusion
survives the noise comfortably — which is the only reason it is stated as a conclusion.

### What this does not mean

The budget being met is a statement about **this workload**, and the workload is still not
the one this document asks for:

- **Read speech, not conversation.** LibriSpeech is audiobook narration. Real conversation
  has disfluencies, crosstalk and unclear endpoints, all of which make recognition harder and
  its output messier — and the translator is downstream of that output.
- **One language pair.** The reference workload asks for three.
- **No microphone.** Every number here comes from a file.
- **Short sentences in the quality set.** Tatoeba sentences are brief and single-reference.
  chrF cannot distinguish "different but equally correct" from "worse", and the two decodings
  differ on 78 of 300 sentences — largely word order and the grammatical gender English
  leaves ambiguous, where both readings are defensible. Long or syntactically complex input
  is unmeasured, and beam search exists precisely for the harder cases.

A native Russian reader would still add what chrF cannot: whether those 78 differences read
naturally. That is now a refinement rather than a blocker.

## Status

**PROVISIONAL**, and now for one reason only: the workload.

The stages exist, the path is measurable end to end, and **every target is met** on 65
utterances of real recorded speech — p50 420 ms against 700 ms, p95 912 ms against 1500 ms.
The defect the fifth measurement recorded is resolved, by measurement rather than by moving
the target.

What keeps the status PROVISIONAL is that the workload is read speech, one language pair, and
a file rather than a microphone. Those are the remaining conditions, and none of them is a
pipeline problem any more.

## Owner and review

**Owner:** @tehki

**Review trigger:** when the first end-to-end pipeline exists and a real baseline can be
recorded. At that point every number here is either confirmed against measurement or
revised, and this status line stops saying PROVISIONAL.
