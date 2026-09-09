# ADR 0040 — The wait before the first word is two waits

**Status:** Accepted
**Date:** 2026-09-09
**Deciders:** @tehki
**Risk:** MODERATE — spends measured memory headroom to buy startup time

## Context

`docs/PERFORMANCE_BUDGET.md` asks for **application start to ready in 3 s, hard limit 6 s**,
and has recorded that startup is over budget since the fourth measurement. What it had not
recorded is how far over, end to end, now that a run can hold three models.

Measured 2026-09-09, wall time from process start to the first text on screen, models already
cached, load average 3:

| configuration | to first text |
| --- | --- |
| `fr→en`, CTranslate2 | 8.1 s |
| `fr→ru` bridged, CTranslate2 | 8.8 s |
| `fr→en`, ONNX | 13.4 s |
| `fr→ru` bridged, ONNX | **21.3 s** |

Almost none of that is this project's own computation. Interpreter start and imports are
**0.17 s** — the lazy imports ADR 0016 asked for are working. The rest is model construction,
performed one model after another, each mostly waiting on a runtime that releases the GIL
while it works.

**They do not depend on each other.** A translation model's construction needs nothing from
the recogniser, and the two legs of a bridged pair ([ADR 0037](0037-french-and-russian-through-english.md))
need nothing from each other.

## Decision

**Build them at the same time.** A `both` helper in `infrastructure/parallel.py` runs two callables on two
threads, waits for both, and returns both — used in three places: the command line's `stream`
and `listen`, the window's worker, and `_open_pivot` for the two legs of a bridged pair.

The wait becomes the longer of the two rather than their sum.

| configuration | before | after | change |
| --- | --- | --- | --- |
| `fr→en`, CTranslate2 | 8.1 s | 7.6 s | −0.5 s |
| `fr→ru` bridged, CTranslate2 | 8.8 s | 8.4 s | −0.4 s |
| `fr→en`, ONNX | 13.4 s | 11.7 s | −1.7 s |
| `fr→ru` bridged, ONNX | **21.3 s** | **16.9 s** | **−4.4 s, −21%** |

Two runs per configuration, alternating between the two versions in one session so the page
cache and the machine's load are the same for both.

**The gain is largest where the wait is worst**, which is the right shape: the bridged pair on
the portable engine is the slowest thing this project does, and it is the configuration a
phone would run.

## What it costs

**37 MB of steady memory and 25 MB of peak**, measured with `scripts/measure_memory.py` on the
worst configuration in the same session:

| | steady | peak |
| --- | --- | --- |
| in series | 1109 MB | 1331 MB |
| at once | 1146 MB | 1356 MB |

Against the 1200 MB target that is **91 MB of headroom becoming 54 MB**. No new objects are
live — the models are the same models — so this is allocator high-water rather than anything
retained: two loads at once leave arenas the process does not return.

That is a real cost and it is being spent knowingly. 4.4 s off the worst wait a user has, for
3% of a budget row that the twenty-seventh measurement measured for the first time last week.
If a later measurement puts that row over, the remedy is available and narrow: load in series
when the engine is ONNX and the pair is bridged, which is the one configuration where both
numbers are extreme.

## What this does not do

- **It does not bring startup inside the budget.** 16.9 s against a 6 s hard limit is still
  nearly three times over. What it removes is the part that was pure serialisation.

  > **What remains, measured rather than inferred (2026-09-09).** This ADR first said "3.5 s
  > of digest verification and about 8 s of session construction per model", which was
  > arithmetic on a total rather than a measurement. Taken directly, for one ONNX export:
  > **2.4 s of digest verification** and **3.5 s to 7.6 s to build the three graphs**, the
  > spread depending on what is already resident — the second model built in a process is
  > consistently slower than the first. Session construction alone, all three graphs, is
  > 2.7 s at `ORT_ENABLE_ALL` and 1.3 s at `ORT_DISABLE_ALL`, so about half of it is
  > optimisation performed at load time on every launch. The verification half is now
  > **1.4 s**: the files are digested several at a time, which changes nothing about what is
  > checked.
- **It does not pre-optimise the ONNX graphs, and that was measured before being refused.**
  About half of ONNX session construction is graph optimisation performed at load time, and
  ONNX Runtime can save the optimised graph and skip it next time. Measured 2026-09-09, all
  three graphs of one export: **3.26 s to build normally, 1.72 s to load a pre-optimised
  copy** — 1.5 s a model. It is not taken, for two reasons that are the same reason. It costs
  **404 MB a model on disk**, and the twenty-eighth measurement had just removed 2.09 GB of
  files nobody reads; and ONNX Runtime warns that a graph optimised above
  `ORT_ENABLE_EXTENDED` *"may contain hardware specific optimizations, and should only be used
  in the same environment"* — which is a poor fit for the engine that exists because it runs
  somewhere else. Recorded so the experiment is not repeated.

- **It does not thread the running application.** Recognition and translation stay on exactly
  the path they were measured on, and ADR 0014's thread settings are untouched. This finishes
  before the first frame of audio is read.
- **It does not change what is loaded, or when it fails.** Both models are still built before
  anything is recognised, so a pair that cannot be served still fails while the caller is
  starting up. A failure in either is raised; the recogniser's is preferred when both fail, so
  the message names the thing a user asked for first.
- **It does not measure a phone.** Two threads on four desktop cores is not two threads on a
  phone's cores, and the memory figure is the one that would move first.

## Review trigger

When the steady-memory row is measured again — 54 MB of headroom is small enough that a fourth
model, or a larger recogniser, would breach it before the latency ever became the problem.
