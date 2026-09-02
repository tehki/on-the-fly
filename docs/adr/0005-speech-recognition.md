# ADR 0005 — faster-whisper for speech recognition, with pinned model weights

**Status:** Accepted, with a recorded performance failure
**Date:** 2026-09-02
**Deciders:** @tehki
**Risk:** MODERATE — 20 packages, native binaries, and weights fetched at runtime

## Context

`SpeechRecognizer` has been a port with no implementation since the audio pipeline was
built. Implementing it is what turns a segmenter into a translator, and it is the largest
supply-chain decision this project makes: the dependency is big, and the *model* is fetched
over the network at runtime, on the user's machine, long after any review happened.

## Decision

Adopt **`faster-whisper` 1.2.1** (MIT) — Whisper via CTranslate2 — behind the existing
port, with model weights **pinned by revision and SHA-256 and verified on every load**.

## Admission review (Article 12)

Evaluated by `pip download --no-deps` and `pip install --dry-run --report` before anything
was installed.

| Criterion | Finding |
| --- | --- |
| **Concrete need** | Yes. Local speech recognition cannot be done from the standard library. |
| **Licence** | MIT. All 19 transitive packages are permissive: MIT, BSD-3, Apache-2.0, MPL-2.0. None copyleft in a way that affects this repository's Apache-2.0. |
| **Publisher** | SYSTRAN / Guillaume Klein; the CTranslate2 lineage is well established. |
| **Transitive footprint** | **20 packages, 265 MB installed.** By far the heaviest thing here. Includes native binaries: ctranslate2, onnxruntime, tokenizers (Rust), av (FFmpeg). |
| **Install scripts** | None. All wheels. |
| **Torch** | **Not required.** `transformers[torch]` is a `conversion` extra this project does not enable — that alone saves multiple gigabytes. |
| **Bundled extra** | Ships Silero VAD v6 as ONNX (1.2 MB), which was going to be a separate dependency decision. It is deliberately **not used**; see below. |

### Alternatives

| Option | Why not |
| --- | --- |
| **Vosk** (Apache-2.0) | Far smaller — ~50 MB models, true streaming — but roughly 25 languages against Whisper's 99. "Speak with anyone worldwide" does not survive that. |
| **openai-whisper** | Requires torch. Multiple gigabytes for the same models. |
| **whisper.cpp bindings** | Smaller and appealing, but the Python bindings are thinner and some platforms need a compiler. Revisit for phase 2 mobile, where the C++ core is the natural fit. |
| **A cloud API** | Settled by ADR 0001. Audio would leave the device and the retention guarantee would become unverifiable. |

## Model weights are the real supply-chain question

The package is reviewed once, in a commit. The *weights* are fetched at runtime, and
nothing about `pip` protects them. So `ModelStore` enforces three rules, all fail-closed:

**An unpinned model is refused.** A `ModelPin` carries the expected SHA-256 of every file.
No digests, no load — regardless of how convenient that would be.

**A mismatch is refused and the file is left alone.** Not deleted, not re-downloaded.
Quietly destroying the evidence of a possible supply-chain event is the wrong reflex
(Article 10), and re-downloading over it would hide that anything happened.

**Downloading is opt-in.** `allow_download` defaults to false, and the CLI needs an explicit
`--allow-download`. Reaching the network is a distinct capability from reading a file that
is already there.

Pins are produced by `scripts/pin_model.py` and land in a reviewed commit, never computed by
the loader. A loader that computed its own expected values would verify nothing.

The shipped pin is `Systran/faster-whisper-tiny` at revision
`d90ca5fe260221311c53c58e660288d3deb8d356`, MIT, 78.2 MB across four files.

**Honest limit: this is trust on first use.** The digests describe what arrived on the
machine that ran the pin script. That protects against later tampering, against a publisher
force-pushing a tag, and against a corrupted download. It does not prove the first download
was the publisher's intent. Committing the pin is what makes independent verification
possible — anyone can run the script and compare.

Model files are `DURABLE_PROJECT_ARTIFACT`: intentionally persistent, carrying nothing
anyone said. Nothing derived from user audio is ever written into that cache.

## The bundled VAD is not used

faster-whisper can run Silero VAD inside `transcribe()` and re-cut the audio it is given.
`UtteranceSegmenter` already decided where utterances begin and end, under bounds this
project can explain and test. Enabling both would put that decision in two places, and the
second one would win without appearing in any of our tests. `vad_filter=False`, deliberately.

Silero remains available behind `VoiceActivityDetector` if the energy detector proves
inadequate — as a replacement for it, not as a second opinion underneath it.

## Consequences — including a failed budget

**Gained.** The pipeline transcribes, locally, with verified weights, at zero marginal cost.
99 languages. No API key, so no secret to leak.

**Accepted costs.** 265 MB of dependencies with native binaries. A 78 MB model download on
first use. The Python floor rose to **3.12**, because numpy 2.5 requires it — the previous
`>=3.11` claim would have been one the project could not honour.

### The measurement, and the problem it found

Measured on this machine, `tiny` / int8 / CPU:

| | |
| --- | --- |
| Model load | 2.25 s, once |
| Recognition, 2 s utterance | ~4.4 s |
| Recognition, 5 s utterance | ~4.1 s |
| End-to-end, 3.9 s file, 2 utterances | 8.9 s — **2.29× real time** |

**Recognition cost is flat in utterance length.** Whisper pads every input to a 30-second
window, so a two-second utterance costs the same as a twenty-second one. That is
architectural, not a defect in this code.

**This misses `docs/PERFORMANCE_BUDGET.md` by roughly 3×.** The budget asks for p95
endpoint-to-caption of 1500 ms; recognition alone is ~4400 ms per utterance, and the
pipeline as a whole runs slower than real time — it cannot keep up with a live conversation
on this machine.

Recorded rather than fixed. Handbook 64A is explicit that the absence of a measured problem
is a reason not to optimise; the presence of one is a reason to understand it before
reaching for a remedy. The plausible remedies — batching, a different backend, GPU, or a
streaming-capable model that is not Whisper — are separate decisions with their own
evidence, and one of them may be that Whisper is the wrong architecture for live use.

Segmentation, by contrast, runs at 0.018× real time. It is not the bottleneck and there is
no case for optimising it.

## Review trigger

Revisit when a latency target must actually be met, when a larger model is pinned, or if
`faster-whisper` becomes unmaintained. The first serious question to answer is whether
Whisper's fixed 30-second window is compatible with live translation at all.
