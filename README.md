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
> Capture works on every input device on the reference machine. The adapter negotiates a
> rate the device accepts and resamples to 16 kHz
> ([ADR 0013](docs/adr/0013-capture-rate-negotiation.md)), and reads through PortAudio's
> callback rather than blocking reads, which was aborting the process on one device
> ([ADR 0015](docs/adr/0015-callback-capture.md)). The audio on that machine is still
> saturated hardware, so recognition from a live microphone remains unverified.

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

**Phase 2 — mobile.** iOS and Android. A port of the edges, not a rewrite of the core
([ADR 0002](docs/adr/0002-desktop-first-delivery.md)).

[ADR 0017](docs/adr/0017-any-hardware.md) surveys what that actually costs. Every dependency
has ARM builds, so **ARM Linux and Apple Silicon should work today** — untested, because no
ARM hardware was available, and a wheel existing is not the same as it running. Phones are a
different question: the recogniser ships Android artefacts, but CTranslate2 has no mobile
support, so mobile translation goes through ONNX Runtime — already a dependency here, with
official Android and iOS builds — rather than a new library.

**That second engine now exists** ([ADR 0018](docs/adr/0018-onnx-translation.md)):
`--translation-engine onnx` runs the same model through ONNX Runtime, behind the same port,
with nothing above `infrastructure/` aware of the difference. It is 2.4–2.7x slower on this
desktop, so CTranslate2 stays the default — the portable engine is for hardware where the
choice is not between two engines but between one and none. **Nothing has been run on a
phone**; capture and the interface are still desktop-only.

## The window

```bash
pip install -r requirements-ui.txt
python -m on_the_fly gui
```

A dark caption window: what is being said in white, the translation under it in green,
partials dimmed so "this may still change" is visible without a word for it.

**There is no scrollback, on purpose** ([ADR 0016](docs/adr/0016-desktop-interface.md)). The
window shows one utterance; the next one replaces it. `docs/RETENTION_POLICY.md` puts it in
a line — *live translation is fine, scrollback is not* — and a history pane would quietly
break the promise the project is arranged around. The cost is real and worth stating: look
away and you miss it. Adding history later needs a record in `docs/EXCEPTIONS.md` with an
owner and an expiry, which is the right amount of friction for a change to what the product
retains.

PySide6 is an **optional extra**. The pipeline, the command line and the whole test suite run
without a GUI toolkit installed.

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
| `src/on_the_fly/infrastructure/translation/` | Pinned translation artefacts and both translators, CTranslate2 and ONNX Runtime |
| `src/on_the_fly/app/` | Composition root and command line |
| `src/on_the_fly/ui/` | The desktop window — logic in `caption.py`, widgets in `window.py` |

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

**It meets its latency budget on an idle machine and sits on the line under heavy load.**
Over 65 utterances, with three of four cores deliberately busy for the loaded column:

| | idle | 3 of 4 cores busy | target |
| --- | --- | --- | --- |
| Endpoint → caption p50 | **332 ms** | **710 ms** | 700 ms |
| Endpoint → caption p95 | **736 ms** | **1662 ms** | 1500 ms |
| Endpoint → caption p99 | 944 ms | 2219 ms | 4000 ms (hard) |

Ten milliseconds over target at p50 under load is within these measurements' variance — *at
the line* is the honest description. A live translator runs on a laptop while its user is
doing other things, so the loaded column is the one that matters.

Decoding is greedy rather than the publisher's beam 6. Measured on Helsinki-NLP's own test
sets against their human references, that costs nothing detectable in either direction —
chrF2 66.62 against 66.56 for `en→ru`, 73.17 against 72.73 for `ru→en` — and runs 2.3–2.4x
faster.

Translation is also bounded to one thread. CTranslate2 defaults to using every core, which
measures **6.9x slower under load** — 2899 ms against 421 ms with three of four cores busy —
because a single translation taking every core gets descheduled. One thread costs about 10%
on an idle machine and is the only setting that meets the budget on a busy one.

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

**A second engine, for hardware CTranslate2 cannot reach** ([ADR 0018](docs/adr/0018-onnx-translation.md)):

```bash
python -m on_the_fly stream recording.wav --translate-to ru --translation-engine onnx
```

Same model, same port, ONNX Runtime instead — the runtime with official Android and iOS
builds. **Both pinned pairs run on both engines.** Measured against the default on 300
sentences of each publisher test set:

| | CTranslate2 | ONNX |
| --- | --- | --- |
| `en→ru`, chrF2 vs human references | **66.62** | 66.33 |
| `ru→en`, chrF2 vs human references | **73.17** | 72.59 |

Roughly **two to three times slower** — the ratio ranged 1.6–3.0x across runs, and a single
decimal would be claiming precision these conditions do not support. The quality gap is the
export rather than the quantisation: the full-precision graphs score the same for 232 MB
more.

Measuring the second direction is what caught a defect in the first: OPUS-MT shares one id
between padding and the decoder start token, and the publisher's `bad_words_ids` forbids
generating it. One sentence in 300 emitted `<pad>` until the token budget ran out — 9.7 s of
work for nothing. `en→ru` never hit it, which is the argument for covering both directions
rather than sampling one.

Its artefact is a third-party conversion, since Helsinki-NLP publish Marian weights and not
an ONNX export. ADR 0009's rule is *pin what the publisher published, never a conversion*,
so the converter is admitted under Article 12 with its own review rather than the rule being
quietly bent.

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
