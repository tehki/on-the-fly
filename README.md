# on-the-fly

Live speech translation. Speak without bounds with anyone worldwide.

> **Status: it translates English and Russian, both directions, live.** Point it at a WAV
> file and it will find the utterances, transcribe them with a local, integrity-verified
> streaming model, and translate the finalised text with `--translate-to`. Both directions
> keep up with the audio — 0.54x real time for English→Russian, 0.26x for Russian→English.
> No other pair has a translation model pinned.
>
> **English now streams faster than real time** (0.399x, first text 1.10 s into the audio),
> using sherpa-onnx with a pinned Apache-2.0 model
> ([ADR 0008](docs/adr/0008-sherpa-onnx-streaming.md)). The other six languages still run
> through Whisper, which pads every utterance to a 30-second window and is several times too
> slow for live use. Measurements in
> [docs/PERFORMANCE_BUDGET.md](docs/PERFORMANCE_BUDGET.md).

## Languages

Seven, at two tiers ([ADR 0007](docs/adr/0007-supported-languages.md)):

Seven: English, Russian, Spanish, Italian, French, Portuguese, German. Each has a published
streaming model, so results appear while you speak. **English and Russian are pinned and
measured**; the other five are named because a model exists, not because one has been
adopted, licence-checked or tested.

**Tajik was the eighth and has been removed** ([ADR 0010](docs/adr/0010-drop-tajik.md)). It
had no streaming model anywhere, no licence-clean batch model this project could load
without a multi-gigabyte dependency, and no licence-clean translation model either. Three
unverified stages behind the word "supported" is not support, and nobody here can read Tajik
well enough to tell when it goes wrong. It comes back when a model does.
>
> The microphone adapter now runs against real hardware: it opens a device, yields
> correctly sized frames at the right cadence, releases on every exit path, and maps real
> ALSA failures to typed errors. What has **not** been verified is capture of *usable*
> audio — the machine it was tested on has an input device that produces saturated,
> DC-offset garbage, and raw `sounddevice` produces the same, so that is the hardware
> rather than the adapter. Recognition from a live microphone remains untested.
>
> One real limitation surfaced: the adapter asks for 16 kHz and does not resample. Both
> named hardware devices refused that rate outright; only the resampling system default
> accepted it. On hardware that does not natively offer 16 kHz, capture will fail with a
> clear error rather than silently degrade — but it will fail.

## What it is meant to be

A translator that runs entirely on your own machine. You speak, it recognises, it
translates, it shows you the result — and nothing you said leaves the device or outlives
the conversation by more than ten seconds.

Three constraints shape everything:

**It runs locally.** Not for speed — because a promise about your speech being deleted is
only worth making if it can be kept, and that stops being true the moment audio is handed
to someone else's server. See [ADR 0001](docs/adr/0001-on-device-inference.md).

**It stays free.** No paid API, no metered service, no free tier that can be withdrawn
later. Local models have no per-request cost and no vendor who can change the terms.

**It forgets.** Transient content — audio, transcripts, translations — lives ten seconds
past its last use and is then deleted, automatically. Live translation is fine; scrollback
and history are deliberate exceptions with owners and expiry dates, not defaults. See
[docs/RETENTION_POLICY.md](docs/RETENTION_POLICY.md).

## Plan

**Phase 1 — desktop.** Windows, macOS, Linux. Python, on-device pipeline, PySide6
interface. Proves the pipeline and the retention module against real audio.

**Phase 2 — mobile.** iOS and Android, inference moved native. A port of the edges, not a
rewrite of the core. See [ADR 0002](docs/adr/0002-desktop-first-delivery.md).

## Repository layout

| Path | Contents |
| --- | --- |
| `CODING_AGENT_*`, `REPOSITORY_GOVERNANCE_*` | The normative policy stack |
| `docs/` | Security, retention, governance, performance, exceptions, ADRs |
| `scripts/` | Validators that enforce the policy stack in CI |
| `tests/` | Tests for those validators |
| `src/on_the_fly/domain/retention/` | The ten-second rule, enforced at runtime |
| `src/on_the_fly/domain/audio/` | Capture, voice activity detection, utterance segmentation |
| `src/on_the_fly/infrastructure/audio/` | Microphone and WAV adapters — the only place PortAudio exists |
| `src/on_the_fly/infrastructure/asr/` | Pinned models and the Whisper recogniser |
| `src/on_the_fly/infrastructure/translation/` | Pinned translation artefacts and the CTranslate2 translator |
| `src/on_the_fly/app/` | Composition root and command line |

## Try it

```bash
python -m on_the_fly segment recording.wav
```

```text
file          recording.wav
format        16000 Hz mono 16-bit
audio         3.90s in 195 frames

2 utterance(s):
  #1   start=   0.00s duration= 2.00s frames=100   ended=SILENCE
  #2   start=   2.10s duration= 1.70s frames=85    ended=SILENCE

wall time     0.050s
real-time     0.0128x  (segmentation only)
invalid       0 frame(s)
retention     clean - nothing retained, no deletion failed
```

Mono 16-bit WAV; the file is not resampled. `--json` gives the same thing machine-readably,
and `--allowed-root` confines the input path when it comes from somewhere less trustworthy
than your own shell.

To stream English — text appears while the speaker is still talking:

```bash
python -m on_the_fly stream recording.wav --allow-download
```

```text
file          recording.wav
language      English (en, streaming)
model         streaming-en (local, verified, Apache-2.0)
model load    2.31s

  [   0.00s partial] AFTER
  [   0.00s partial] AFTER EARLY
  [   0.00s final  ] AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP

audio         6.62s in 331 frames
wall time     2.64s
real-time     0.399x  (keeps up)  excludes model load
first text    1.10s into the audio
events        16 partial, 1 final
retention     clean - nothing retained, no deletion failed
```

Only languages with a pinned streaming model are accepted. A language without one is
refused rather than silently downgraded to batch latency — being told "no, use transcribe"
is better than wondering why it is slow.

Add `--translate-to ru` and finalised text is translated as well:

```text
translation   opus-mt-en-ru (local, verified, CC-BY-4.0)
attribution   English-Russian translation by OPUS-MT (Helsinki-NLP), model opus-2020-02-11,
              licensed CC-BY-4.0. https://github.com/Helsinki-NLP/Opus-MT

  [   0.00s final  ] AFTER EARLY NIGHTFALL THE YELLOW LAMPS WOULD LIGHT UP HERE AND THERE
           → После раннего наступления темноты, желтые лампы загорались здесь и там

translation   1 of 1 final(s), median 1476ms, max 1476ms
```

**It meets its latency budget on an idle machine and misses it on a busy one**, and the
second is the condition that matters. Over 65 utterances per direction:

| | idle machine | ~3/4 cores loaded | target |
| --- | --- | --- | --- |
| Endpoint → caption p50 | 420 ms | **1050 ms** | 700 ms |
| Endpoint → caption p95 | 912 ms | **2264 ms** | 1500 ms |

Recognition is unaffected either way (0.33x real time, and 0.098x for Russian); translation
is the whole difference. A live translator runs on a laptop while its user is doing other
things, so the loaded figure is the honest one — and by it, the budget is missed.

Decoding is greedy rather than the publisher's beam 6. Measured on Helsinki-NLP's own test
sets against their human references, that costs nothing detectable in either direction —
chrF2 66.62 against 66.56 for `en→ru`, 73.17 against 72.73 for `ru→en` — and runs 2.3–2.4x
faster. That comparison is within-run, so load affects both arms equally and the decision
stands.

See [docs/PERFORMANCE_BUDGET.md](docs/PERFORMANCE_BUDGET.md).

The other direction streams too ([ADR 0012](docs/adr/0012-russian-streams-after-all.md)):

```bash
python -m on_the_fly stream recording.wav --language ru --translate-to en --allow-download
```

`transcribe --translate-to` remains available for any language without a streaming pin.

**Partials are never translated** ([ADR 0009](docs/adr/0009-translation.md)). Translating
text that is about to be revised costs an inference per partial — sixteen on the sample
above — and produces a caption that rewrites itself. So the source caption streams and the
translation arrives when the speaker stops. That is prompt translation, not live
translation, and it should not be described as the latter.

The model is Helsinki-NLP's own OPUS-MT release, pinned by URL and SHA-256 and converted
locally; the conversion is a derived cache and is never what gets trusted. Its licence is
CC-BY-4.0, which requires attribution, which is why the attribution line is printed rather
than buried in a source file.

For the batch engine, first fetch the pinned model (78 MB, once):

```bash
python -m on_the_fly transcribe recording.wav --allow-download
```

```text
file          recording.wav
model         tiny (local, verified)
audio         3.90s

  [   0.00s +2.00s] good morning, how are you
  [   2.10s +1.70s] very well thank you

wall time     8.94s
recognition   8.90s of that
real-time     2.29x
```

The model is pinned by revision and SHA-256 in
[`models.py`](src/on_the_fly/infrastructure/asr/models.py) and verified on every load. A
file that does not match is refused and left in place for inspection — never silently
re-downloaded. Downloading is off unless you ask for it.

The last line is the one worth reading. Every run states whether it finished holding
nothing, and exits non-zero if it could not delete what it held.

## Working here

```bash
make check
```

On Windows, where `python` is often a Store stub:

```bash
make PYTHON=py check
```

That runs the same gates as CI, in the same order: policy validation, governance
validation, lint, type check, tests.

Start with [docs/CODING_AGENT_ADOPTION.md](docs/CODING_AGENT_ADOPTION.md). It explains the
policy stack, what it enforces, and — more usefully — what it does not yet enforce.

The rule worth internalising before anything else: do not state that a file, test, control,
branch rule, or CI result exists or passed unless you have inspected or executed it.

## Licence

Apache-2.0. Model weights carry their own licences and are reviewed individually; several
widely used multilingual models are non-commercial and are excluded for that reason.
