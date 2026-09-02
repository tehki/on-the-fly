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

## Owner and review

**Owner:** @tehki

**Review trigger:** when the first end-to-end pipeline exists and a real baseline can be
recorded. At that point every number here is either confirmed against measurement or
revised, and this status line stops saying PROVISIONAL.
