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

## Seventh measurement — 2026-09-04, both directions

Russian streams now (ADR 0012), so both pinned pairs can be measured, and one hole in the
previous measurement needed closing.

### The hole: a global default validated on half the product

The sixth measurement changed the decoding default to greedy for **every** language pair,
on evidence from `en→ru` only. `ru→en` was never checked. That is a global change justified
by a local measurement, and it is exactly the shape of error this document keeps recording.

Checked now, same method — 300 sentences from the publisher's own `ru→en` test set, chrF2
against their human English references:

| | chrF2 | Latency p50 |
| --- | --- | --- |
| beam 6 (publisher default) | 72.73 | 377 ms |
| **beam 1 (greedy)** | **73.17** | **155 ms** |
| Published score for this model | 73.6 | — |

Validated the same way before being believed: our beam-6 output reproduces the publisher's
own hypotheses at chrF2 **96.1**, and scores **72.73** against their references where they
published **73.6**.

**The decision holds, and holds slightly better in this direction** — greedy is 0.44 ahead,
at 2.44x the speed, with 249 of 300 outputs identical. Both pairs are now covered by the
evidence that justified the default.

### Coverage against the reference workload

| Requirement | en→ru | ru→en |
| --- | --- | --- |
| End-to-end distribution, real speech | **65 utterances** — p50 420 ms, p95 912 ms | **1 utterance** — 871 ms, 0.258x real time |
| Translation stage distribution | 300 sentences — p50 153 ms, p95 346 ms | **300 sentences** — p50 155 ms, p95 275 ms |
| Quality against human references | 66.62 chrF2 | 73.17 chrF2 |
| Source text | real (LibriSpeech transcripts) | **real human Russian** (publisher test set) |

The `ru→en` translation figures are a genuine improvement on the fifth measurement, which
used Russian produced by the `en→ru` model and was therefore a best case. These are human
Russian sentences.

**`ru→en` end to end is one utterance and no percentile is claimed for it.** One sample is
what the fourth measurement got wrong; the correction is not to repeat it in the other
direction. A Russian speech corpus was not obtained — the model publisher ships a single
sample, and the larger Russian sets are either behind acceptance terms or too large to be
proportionate here.

### What is still missing, and why

- **Two language pairs, not three.** Adding a third means adopting a translation model,
  checking its licence, and deciding which language the product serves next. That is a
  product decision with a permanent consequence, not a measurement task, and doing it to
  satisfy the shape of a benchmark would be the wrong reason. Recorded as a gap rather than
  closed badly.
- **Read speech, not conversation**, for the pair that has a distribution.
- **No microphone.** The adapter has now been run against real hardware and works; the
  hardware available produced saturated, DC-offset audio, so recognition from a live
  microphone is still unverified. See `docs/CODING_AGENT_ADOPTION.md`.

## Eighth measurement — 2026-09-04, both pairs, and a result that did not reproduce

Both pinned pairs, 65 utterances each, same code path and the same endpoint-to-caption
definition. Two things came out of it: the first real `ru→en` distribution, and the
discovery that the sixth measurement's headline does not hold.

### The corpora are no longer comparable, deliberately

| | `en→ru` | `ru→en` |
| --- | --- | --- |
| Corpus | LibriSpeech dev-clean | SOVA RuDevices |
| Kind of speech | audiobook narration, read | **crowdsourced device recordings, spontaneous** |
| Difficulty | clean acoustics, tidy endpoints | disfluencies, real rooms, real microphones |

The Russian side is *harder* input than the English side. The two columns below are not a
league table and a difference between them says as much about the corpus as the pipeline.
The `ru→en` figures are the first here measured on genuinely spontaneous speech, which is
closer to what this document's reference workload asks for than the English side manages.

### Both pairs, measured

| | `en→ru` | `ru→en` | Target | Hard limit |
| --- | --- | --- | --- | --- |
| Endpoint → caption p50 | 1050 ms | 853 ms | 700 ms | — |
| Endpoint → caption p95 | 2264 ms | 1675 ms | 1500 ms | 2500 ms |
| Endpoint → caption p99 | 4775 ms | 2847 ms | — | 4000 ms |
| Mean | 1246 ms | 961 ms | — | — |
| Recognition real-time factor | 0.33x | **0.098x** | under 1.0x | — |

Recognition keeps up comfortably in both directions; the Russian model is three times
faster than the English one relative to its audio. **Both pairs miss p50 and p95**, and
`en→ru` exceeds the 4000 ms p99 limit at 4775 ms.

### The sixth measurement does not reproduce

This is the part worth reading. The sixth measurement reported `en→ru` at **p50 420 ms,
p95 912 ms** and concluded that every target was met. Re-run today on the same corpus, the
same 65 utterances, the same beam setting and the same code:

```text
                    sixth measurement    today       today, isolated
p50                        420 ms       1050 ms          1074 ms
p95                        912 ms       2264 ms          2390 ms
recognition RTF p50        0.333        0.332            0.334
```

**2.5x slower, and it reproduces.** Two independent runs today agree with each other and
disagree with the earlier one.

The diagnosis is in the third row. **Recognition is unchanged** — 0.332 against 0.333, the
same to three decimal places. Only translation moved. So this is not the machine being
uniformly slower: it is CTranslate2 contending for cores in a way that ONNX Runtime, capped
at two threads, does not. The machine today carries a load average of ~3 on 4 cores; the
sixth measurement had it quiet.

### What that costs the sixth measurement's conclusion

The claim was: *"Every target in this document is now met on this workload... The margin is
far larger than the ±20% variance these measurements carry, so the conclusion survives the
noise comfortably."*

The margin was not larger than the variance. **The variance was 2.5x, not ±20%**, and the
±20% figure was itself an estimate made from two runs that happened to be taken under
similar conditions. The conclusion did not survive.

What survives is narrower and still useful: **greedy decoding is 2.3-2.4x faster than beam 6
under any load measured**, and that comparison was always within-run, so contention affects
both arms equally. The decoding decision stands. The budget verdict does not.

### The finding underneath

An idle-machine measurement of a live translator is measuring a condition the product will
rarely be in. This runs on someone's laptop while they are in a call, with a browser open,
possibly with other models loaded — the machine that produced today's numbers was itself
running a local LLM server. **That is the realistic condition, and under it the budget is
missed.**

So the honest statement is conditional, and the condition has to travel with the number:

```text
idle machine        p50  420 ms   p95   912 ms   budget met
~3/4 cores loaded   p50 1050 ms   p95  2264 ms   budget missed
```

Neither number is wrong. Quoting only the first one would be.

### What this changes

Nothing in the code. The budget is recorded as **missed under load**, which under this
document's own rules is a defect rather than a target to revise. Concurrency limits for the
translator — CTranslate2 exposes `inter_threads` and `intra_threads`, both currently
unset — are the obvious next lever, and are unmeasured. That is a measurement, not a guess,
and it has not been done.

Every future measurement in this document states the load it was taken under. The absence
of that field is what let the sixth measurement overclaim.

## Ninth measurement — 2026-09-04, bounded threads, and a bug the measurement exposed

The eighth measurement found the budget missed under load, established that the whole gap
was translation, and named CTranslate2's unset thread settings as the obvious untested
lever. Tested now, under load created deliberately rather than hoped for.

### The sweep

120 sentences, 4 cores, greedy decoding. The loaded rows have three of four cores
deliberately occupied, so the condition is stated and reproducible — which is precisely
what the sixth measurement could not say about itself.

| | idle p50 | idle p95 | **loaded p50** | **loaded p95** |
| --- | --- | --- | --- | --- |
| all cores (the default) | 176 ms | 283 ms | **2899 ms** | **4464 ms** |
| `intra_threads=1` | 193 ms | 300 ms | **421 ms** | **636 ms** |
| `intra_threads=2` | 144 ms | 233 ms | 1120 ms | 2718 ms |
| `intra_threads=4` | 234 ms | 349 ms | 2952 ms | 4734 ms |

**The default is 6.9x slower under load than a single thread.** Letting one translation
grab every core is fine on an idle machine and catastrophic on a busy one: the work gets
descheduled and synchronisation overhead dominates.

`intra_threads=1` is the only setting that **meets the budget in both conditions** —
p50 193/421 ms against a 700 ms target, p95 300/636 ms against 1500 ms. It costs about 10%
when nothing else is running. `intra_threads=2` is the fastest idle and misses p50 under
load by 60%, which makes it the wrong default for a translator that runs while its user is
doing other things.

**Adopted: `intra_threads=1`.**

### The bug this exposed

Setting the parameter meant reading `load()`, which is how this came to light:

```text
OpusMtTranslator.__init__   beam_size = 1     # changed by the sixth measurement
load()                      beam_size = 6     # never changed, and passed explicitly
CLI                         load_translator(...)  # no beam_size argument
```

**Every translation the application performed used beam 6.** The greedy decoding adopted by
the sixth measurement never reached the product. The test asserting greedy passed because it
constructed `OpusMtTranslator` directly — it tested the unit and not the path anyone uses,
which is the same shape as the uppercase seam in ADR 0009: two correct pieces and a wrong
join.

Both defaults now come from one constant, and a test compares the two signatures so a future
divergence fails rather than ships.

### What that costs the earlier numbers

| Measurement | What it actually ran | Still true? |
| --- | --- | --- |
| Sixth — quality comparison, beam 6 vs 1 | Explicit `beam_size`, both arms | **Yes.** Passed the value directly, so the comparison was real |
| Sixth — end-to-end "budget met", p50 420 ms | Explicit `beam_size=1` | **Yes**, as a beam-1 measurement of an idle machine |
| Eighth — both pairs under load | `load_translator()` with no argument, so **beam 6** | The numbers are right; the label was wrong |

So the eighth measurement's p50 1050 ms was beam 6 under load, not greedy under load. It
correctly described what the application did — it just did not describe what this document
claimed the application did. The defect was in the code and the claim, not in the
measurement.

### Not re-run

End-to-end endpoint-to-caption has **not** been re-measured with both fixes in place. The
translation stage is the whole gap by the eighth measurement's own diagnosis, and the stage
now measures 421 ms p50 under load against a 700 ms target — but that is an inference, and
this document has been wrong before by treating one as a result. The end-to-end figure
stands unmeasured until it is measured.

## Tenth measurement — 2026-09-04, end to end with both fixes

The ninth measurement deliberately refused to claim the budget was met: it had the
translation stage at 421 ms under load and the eighth measurement's diagnosis that the stage
was the whole gap, but that was an inference. Measured now, through `load()` with no
arguments — the shipped defaults, greedy and single-threaded, on the path the application
actually takes.

65 utterances of real recorded speech per condition, load created deliberately:

| | idle | **loaded (3 of 4 cores busy)** | Target | Hard limit |
| --- | --- | --- | --- | --- |
| Endpoint → caption p50 | **332 ms** | **710 ms** | 700 ms | — |
| Endpoint → caption p95 | **736 ms** | **1662 ms** | 1500 ms | 2500 ms |
| Endpoint → caption p99 | 944 ms | 2219 ms | — | 4000 ms |
| Recognition real-time factor | 0.308x | **0.848x** | under 1.0x | — |

**Idle: every target met, comfortably** — p50 332 ms against 700, p95 736 ms against 1500.

**Loaded: p50 710 ms against a 700 ms target.** Ten milliseconds over, which is 1.4% and well
inside the variance these measurements carry; the honest description is *at the line*, not
met and not missed. p95 1662 ms misses the 1500 ms target by 11% while staying inside the
2500 ms hard limit. p99 is comfortably inside its limit.

### Against the eighth measurement

Same corpus, same method, same 65 utterances; beam 6 and unbounded threads then, greedy and
one thread now:

| | eighth (beam 6, all cores) | **tenth (greedy, 1 thread)** | |
| --- | --- | --- | --- |
| p50 | 1050 ms | **710 ms** | 1.5x |
| p95 | 2264 ms | **1662 ms** | 1.4x |
| p99 | 4775 ms | **2219 ms** | **2.2x**, and back inside the hard limit |

The p99 improvement matters most: 4775 ms was over the 4000 ms limit, which is the threshold
this document calls a dropped turn. It no longer is.

### An assumption that did not survive

The eighth measurement recorded recognition as *unaffected* by load — 0.41x against 0.33x —
and concluded the entire gap was translation. Under the load generated here, recognition runs
at **0.848x against 0.308x, a factor of 2.75.**

Both are true measurements of different loads. The eighth measured whatever the machine
happened to be doing; this one pins three of four cores. **"Recognition is unaffected by
load" was a statement about one particular load**, and it was generalised further than the
evidence reached — the same error this document has now recorded three times, in three
different forms.

Recognition still keeps up at 0.848x, but the margin is thin. A busier machine, or one with
fewer cores, would push it past 1.0x and the pipeline would fall behind the speaker.

### Why the end-to-end figure is above the stage figure

The ninth measurement had the translation stage at 421 ms p50 under load; this measures
710 ms end to end under the same load. The difference is **sentence length**, not an error in
either: the stage sweep used OPUS test sentences with a median of 85 characters, while
LibriSpeech utterances of 3–15 seconds are considerably longer, and translation cost scales
with tokens.

Which is the reason the ninth measurement declined to infer this number from that one.

## Eleventh measurement — 2026-09-04, how much hardware keeping up needs

The tenth measurement flagged recognition at 0.848x under load as the thinnest margin in the
pipeline: past 1.0x the recogniser falls behind the speaker, and then nothing downstream
matters. That figure came from one machine at one core count, using the command line's
default of **two** recogniser threads.

`taskset` to constrain cores, `num_threads` varied, 23 s of real speech, three passes:

| `num_threads` | 1 core | 2 cores | 4 cores |
| --- | --- | --- | --- |
| **1** | **0.307** | **0.337** | **0.315** |
| 2 (was the default) | 0.779 | 0.740 | 0.470 |
| 4 | 1.604 ✗ | 1.378 ✗ | 1.181 ✗ |

**One thread is fastest at every core count**, and four threads fall behind real time at all
of them — including with four cores, the configuration that should suit them best.
Transcripts are byte-identical across all three, so this is speed at no quality cost.

**The worry was the wrong variable.** The tenth measurement asked whether a weaker machine
would push recognition past real time and treated core count as the risk. It was the thread
count: at one thread, recognition keeps up on a *single* core at 0.307x, with more headroom
than the four-core machine had at its old default.

Adopted in [ADR 0014](adr/0014-single-threaded-inference.md), which makes both models in the
pipeline single-threaded — the same conclusion the ninth measurement reached for translation,
for the same reason. Streaming work arrives in 20 ms chunks, and coordinating threads costs
more per chunk than it saves.

**Not re-measured:** the tenth measurement's end-to-end figures were taken at two recogniser
threads. They should improve, and that is an expectation rather than a result until someone
runs it.

## Twelfth measurement — 2026-09-04, the second engine

ADR 0018 added a second implementation of the `Translator` port on ONNX Runtime, because
CTranslate2 has no mobile build and this product has to run on a phone (ADR 0017). Two
engines running the same model raise exactly the question this document exists to answer:
what does the portable one cost?

Both were loaded through the application's own factory — the path the CLI takes, greedy and
single-threaded — and given the same 300 sentences from Helsinki-NLP's `en-ru` test set.

### Quality: 0.29 chrF2 apart, and it is not quantisation

| | chrF2 vs human references |
| --- | --- |
| CTranslate2, int8 (the default) | **66.62** |
| ONNX Runtime, int8 | **66.33** |
| ONNX Runtime, **fp32** | **66.34** |
| Published score for this model | 66.9 |

The CTranslate2 figure reproduces the sixth measurement's 66.62 **exactly**, on a rebuilt
model directory a day later. That reproduction is what makes the rest of the table worth
reading.

**The full-precision graphs score 66.34 against the quantised 66.33** — no difference — for
653 MB against 421 MB. So the 0.29 that separates the two engines is not the quantisation;
it is the export, and it does not shrink by paying 232 MB more. Checking that cost one
download and three minutes, and it replaced an assumption this document would otherwise have
carried indefinitely.

Agreement between the engines: **239 of 300 sentences identical (79.7%)**, chrF2 94.1
between their outputs. The disagreements are of the kind quantisation produces — `чёрное`
against `черное`, one gender ending, an equivalent alternative — rather than a different
model.

### Latency: the portable engine is 2.4–2.7x slower

300 sentences per cell, one process, load created deliberately for the loaded columns. The
"idle" columns are *no deliberate load*, with a 1-minute load average of 1.9–2.6 on this
4-core machine — this is the same machine the eighth measurement caught running a local LLM
server, and calling it idle without that qualifier is the error this document has recorded
three times.

| | CTranslate2 idle | ONNX idle | CTranslate2 loaded | **ONNX loaded** |
| --- | --- | --- | --- | --- |
| Translation stage p50 | 144 ms | 383 ms | 277 ms | **674 ms** |
| p95 | 235 ms | 647 ms | 572 ms | **1259 ms** |
| p99 | 418 ms | 1088 ms | 895 ms | **2377 ms** |
| Model load | 0.99 s | **9.18 s** | — | — |

**2.7x at p50 idle, 2.4x under load.** The ratio holds across conditions, which is the part
worth trusting: both arms ran in one process minutes apart, so contention hits them equally.

Two consequences follow, and neither is a reason to change the default:

- **The stage alone nearly exhausts the end-to-end budget under load.** 674 ms p50 for
  translation against a 700 ms endpoint-to-caption target leaves nothing for recognition, and
  the tenth measurement puts the whole pipeline at 710 ms on CTranslate2 under the same load.
  On this hardware, the ONNX engine would miss the budget under load.
- **Model load is 9.18 s against a 3 s startup target** — worse than the 5.35 s recognition
  load the fourth measurement recorded, and it varied from 5.6 s to 9.2 s across runs here.

**CTranslate2 stays the default**, which is what ADR 0018 decided and what these numbers
support. ONNX exists for the hardware where the choice is not between two engines but
between one engine and none.

### What this does not measure

**None of it ran on a phone**, which is the entire reason the second engine exists. A phone
is not a slower laptop: different cores, different memory bandwidth, different thermal
behaviour, and — for ONNX Runtime specifically — different execution providers (NNAPI,
Core ML) that are not in play here at all. These numbers say what the portable engine costs
*on this desktop*, and that is the only claim they support.

The disk cost is also real and unmeasured against any device constraint: **421 MB of ONNX
graphs against 84 MB for the CTranslate2 conversion of the same model**, two thirds of it
the decoder weights carried twice because the merged decoder graph is broken on its no-cache
path (ADR 0018). On a phone that is a product decision, not a footnote.

## Thirteenth measurement — 2026-09-04, the other direction, and what it caught

The twelfth measurement covered `en→ru` on the second engine and stopped there. The seventh
measurement already recorded what that costs — *a global change justified by a local
measurement* — so `ru→en` was pinned on ONNX and measured the same way. It found a defect
that the first direction could not have found.

### The defect

One sentence in 300 came back as `<pad>` repeated until the token budget ran out: **9.7 s of
work for output that was pure padding.** Others carried a stray `<pad>` mid-sentence.

OPUS-MT uses one id (62517) for both padding and the decoder start token, and the
publisher's `generation_config.json` forbids generating it (`bad_words_ids`). `transformers`
applies that automatically; the hand-written loop that exists to keep torch off a phone
(ADR 0018) did not. It does now, `generation_config.json` is part of the pin, and a
multi-token constraint is refused rather than silently skipped.

```text
Сошлитесь на мою предыдущую статью.
  before   <pad><pad><pad>... to the 256-token cap    9726 ms
  after    Please refer to my previous article.        288 ms
```

**`en→ru` never hit it in 300 sentences.** Measuring one direction would have found nothing,
and the quality figure that direction produced was correct both before and after the fix —
66.33 either way. The bug was reachable from the first commit and only the second direction
surfaced it.

### Quality, both pairs, both engines

| | CTranslate2 | ONNX | gap |
| --- | --- | --- | --- |
| `en→ru`, chrF2 vs human references | **66.62** | 66.33 | 0.29 |
| `ru→en`, chrF2 vs human references | **73.17** | 72.59 | 0.58 |

Both CTranslate2 figures reproduce this document's own earlier numbers exactly — 66.62 from
the sixth measurement, 73.17 from the seventh — which is the only reason the ONNX column is
worth reading. The fix moved `ru→en` from 72.25 to 72.59 and lifted engine agreement from
253 to **255 of 300 identical**, chrF2 95.0 between the two engines' outputs.

### Latency, and an honest note about its spread

`ru→en`, 300 sentences, three of four cores deliberately busy for the loaded columns:

| | CTranslate2 idle | ONNX idle | CT2 loaded | ONNX loaded |
| --- | --- | --- | --- | --- |
| p50 | 113 ms | 336 ms | 217 ms | **627 ms** |
| p95 | 188 ms | 580 ms | 435 ms | **1122 ms** |
| p99 | 336 ms | 970 ms | 798 ms | 2032 ms |
| max | 1003 ms | **2627 ms** (was 10373 before the fix) | 1976 ms | 5548 ms |

**CTranslate2 was faster in every arm measured, and the ratio is less stable than the
twelfth measurement implied.** Across four runs today it ranged **1.6x to 3.0x**, and the
variance sits in the CTranslate2 arm — its `en→ru` idle p50 came out at 144 ms in one run
and 224 ms in another, while ONNX moved by 4%. Each arm runs first in its own process and
picks up whatever the machine is doing at that moment.

So the defensible statement is the direction and the rough size: **the portable engine costs
roughly two to three times the latency**, and quoting a single decimal for that ratio would
be claiming precision these conditions do not support. The conclusion that matters is
unchanged and survives the spread easily — CTranslate2 stays the default; ONNX is for
hardware where the alternative is nothing.

### End to end, both directions on both engines

`ru→en` on the Russian sample published with the pinned recogniser, through `stream`:

```text
ctranslate2   Rodon of the poppist counted every new creep and long ago determined
onnx          Rodon poppist counted every new piece of depth and long ago determined
```

0.252x real time on ONNX, retention clean on both. Recognition drops the proper noun in
both — that is the recogniser, not the translator.

**Still nothing on a phone.** Every number in this section is a desktop number.

## Fourteenth measurement — 2026-09-05, the merged decoder, and a claim that was wrong

ADR 0018 recorded that the export's merged decoder "fails on a zero-length encoder cache",
and used that to justify shipping two decoder graphs at a cost of **183 MB of duplicated
weights**. The claim went into an ADR, a module docstring, a source comment and the README
on the strength of one failure diagnosed once.

**It was wrong.** The merged graph decodes all 300 test sentences.

### What actually happens

On its **cached** branch the merged decoder returns placeholder `present.*.encoder.*`
outputs of shape `(0, 8, 1, 64)`. A loop that copies every `present.*` back into its cache —
the obvious thing to write — feeds that placeholder in one step later, and *that* raises the
`Reshape` error on `encoder_attn`. The traceback names the encoder cache, which is why the
first reading was "the no-cache path cannot handle an empty encoder cache".

The rule that avoids it is the one the shipped loop already followed for its own reasons:
the encoder half of the cache is written once and never overwritten. It followed it by
accident — the split with-past graph simply does not declare those outputs — so the rule is
now structural: the two halves are separated at construction, only the decoder half is
updated, and a cache input naming neither half is refused rather than guessed at.

### What the merged graph costs, now that it runs

300 sentences, `en→ru`, greedy, single-threaded, one process:

| | two graphs (shipped) | merged |
| --- | --- | --- |
| Decoder graphs on disk | 370 MB | **187 MB** |
| Total artefact | 421 MB | **238 MB** |
| chrF2 vs human references | **66.33** | 65.82 |
| p50 | **323 ms** | 633 ms |
| p95 | **540 ms** | 1102 ms |
| Identical output | — | 263 of 300 |

**Twice the latency to save 183 MB, and 0.51 chrF2 worse.** The quality difference comes
from the merged export being quantised as its own graph — same weights, different rounding —
which also explains the 37 sentences that differ.

**Not adopted.** Latency is this engine's binding constraint: it is already two to three
times CTranslate2, and the twelfth measurement showed the ONNX stage alone consuming the
endpoint-to-caption budget under load. Doubling it to halve a download is the wrong side of
that trade while the target is a desktop. On a phone the trade is different, and it is
recorded with numbers so that revisit starts from evidence.

**The mechanism behind the 2x was not established.** The merged graph binds
`encoder_hidden_states` on every step and evaluates an `If` node the split pair does not
have; either could dominate. What is established is the cost as the interface presents it,
which is what the decision needed — and saying which of those two facts is which is the
whole point of this section.

## Status

**PROVISIONAL.** The budget is **met on an idle machine and sits on the line under heavy load** — p50 710 ms against a 700 ms target, p95 1662 ms against 1500 ms with the hard limit intact.

The stages exist and the path is measurable end to end in both directions. With greedy
decoding and bounded threads actually shipping, the tenth measurement puts p50 at 332 ms
idle and 710 ms with three of four cores busy, against a 700 ms target — met comfortably in
the first condition and within 1.4% in the second. p99 is back inside its hard limit, which
it was not before.

What keeps the status PROVISIONAL: two language pairs rather than three, read speech on the
English side, no microphone, and no controlled load environment. `ru→en` now has a real
distribution on spontaneous speech, which closes the gap the seventh measurement recorded.
One remaining gap is a product decision, one is hardware, and one — a quiet machine — is
what the eighth measurement shows matters most.

## Owner and review

**Owner:** @tehki

**Review trigger:** when the first end-to-end pipeline exists and a real baseline can be
recorded. At that point every number here is either confirmed against measurement or
revised, and this status line stops saying PROVISIONAL.
