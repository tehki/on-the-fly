# on-the-fly

Live speech translation. Speak without bounds with anyone worldwide.

> **Status: it runs, but it does not translate yet.** You can point it at a WAV file and
> it will find the utterances in it. What is missing is the part that makes it a
> translator: speech recognition and translation, each a dependency decision not yet made.
>
> The microphone adapter has not been run against a real microphone — the machine it was
> written on has output devices only. Its logic, error mapping and device enumeration are
> tested; an actual capture is not. Nothing here should be read as a claim beyond that.

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
