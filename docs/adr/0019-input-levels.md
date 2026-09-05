# ADR 0019 — Telling the user when the microphone is unusable

**Status:** Accepted
**Date:** 2026-09-05
**Deciders:** @tehki
**Risk:** LOW — one pure domain module, one decorator, one line of interface

## Context

ADR 0015 and ADR 0016 both recorded the same unresolved sentence: *the reference machine's
microphone produces saturated audio*, so the window "has never captured live speech". It was
recorded as a hardware inconvenience and left there.

It is worth more than that, because of what the pipeline does with such audio: **it
transcribes it.** The recogniser is handed a square wave, emits words that were never spoken,
and the translator makes fluent Russian out of them. Every stage does its job correctly on
the input it was given. Nothing anywhere notices, and the user sees confident output with no
reason to distrust it — in a language they very likely cannot check.

That is the worst failure shape this product has: not silence, not an error, but plausible
text nobody said.

## What was actually wrong with the machine

Measured before anything was built, because "the microphone is bad" is not a diagnosis:

```text
                       peak    rms      clipped samples
recorded speech (en)   0.535   0.0471   0.0000%
recorded speech (ru)   0.500   0.0790   0.0000%
this machine's mic     1.000   0.8134   51.04%
```

**The first hypothesis was wrong.** `sounddevice` reports the default input device as index
12 while the only device with input channels is 13, so the obvious explanation was that the
wrong device was being opened. Probed explicitly: device 13 gives the same saturated signal
(peak 1.000, 51.04% clipped, DC offset −0.008 against the default's −0.230). Refuted.

The cause is in the system mixer, not in this project:

```text
$ amixer -c 0 sget Capture
  Front Left:  Capture 63 [100%] [30.00dB] [on]
  Front Right: Capture 63 [100%] [30.00dB] [on]
```

**Capture gain pinned at +30 dB.** Everything, including the room's noise floor, is driven
into the rails. It is a ten-second fix *for a user who is told about it*, and nothing told
them.

## Decision

**Measure the input and say so, in the terms the person can act on.**

```text
domain/audio/levels.py   LevelMonitor        peak, rms, clipped fraction -> a verdict
                         LevelWatchingSource an AudioSource that measures what passes through
ui/                      one amber line      "turn its input gain down"
app/cli.py               one line after a run, for recordings
```

The monitor is pure and lives in the domain, so it is tested without a device — the same
reason `EnergyVoiceActivityDetector` lives there. The decorator sits on the `AudioSource`
port, which is the one place where "the frames the pipeline reads" and "the frames to
measure" are the same object; nothing in the microphone adapter changes, and a WAV file gets
the same check for free.

### The thresholds, and why they are where they are

| Verdict | Rule | Why there |
| --- | --- | --- |
| `CLIPPING` | ≥ **5%** of samples in the window at full scale | Speech measures 0.0000%; the broken machine 51%. A single fully-clipped 20 ms frame — a door slam, a knock on the desk — is 2% of a one-second window, and a warning that flickers on every loud noise is one people learn to ignore |
| `SILENT` | peak < 0.002 | Digital silence: muted, or a device that is not the microphone |
| `QUIET` | rms < 0.005 | An order of magnitude below the quietest recorded speech measured here (0.047) |
| `OK` | otherwise | |

Clipping is checked first. A silent or quiet input produces no transcript or an obviously
poor one, which a user can see for themselves; distortion produces fluent, wrong text, which
they cannot.

### Two verdicts, not one

`reading` is the rolling window — *is the microphone bad right now* — and it is what the
window shows, so a user who turns their gain down watches the warning clear.

`overall` covers everything since the last reset, and it is what a finished recording is
judged on. The first version had only the window, and reported **nothing** for a file that
was clipped from end to end: the last second of a recording is its silent tail, and the
verdict was taken there. Both are now tested against that exact case.

## Retention

`LevelReading` is four numbers. The monitor holds counters and a bounded deque of per-frame
counters — **no audio, at any point** — and frames pass through the decorator unchanged
rather than being copied or buffered. Under `docs/RETENTION_POLICY.md` these are
`OPERATIONAL_METADATA`: safe to display and safe to log, which captured audio is not. A test
asserts the decorator holds no bytes.

## What this does not do

- **It does not fix the gain.** Changing a user's system mixer is a side effect an
  application should not take on its own — they may be in a call on the same device. The
  scope is telling them precisely what is wrong and what to change.
- **It does not verify live speech recognition.** That still needs a microphone that works,
  and this machine's does not until someone turns the gain down at the mixer. What is now
  true is that the application *says so* rather than transcribing the noise.
- **It does not mean clipped audio is worthless.** A deliberately clipped copy of the test
  sample (12x gain, 7.9% of samples at full scale) still transcribed **correctly**. Mild
  clipping degrades gracefully; 51% is not mild. The line is advice, not a refusal, and it is
  worded that way.
- **No device picker.** `MicrophoneSource` already accepts a device and nothing exposes it.
  That is the natural next change and is not this one — and on this machine it would not have
  helped, which is exactly why the hypothesis was tested before being built on.

## Consequences

- The window shows one amber line above the status: *the microphone is too loud and the
  audio is distorting — turn its input gain down*. It takes priority over the dropped-blocks
  warning, because dropped blocks lose words a user can notice missing.
- `stream` prints an `input` line after a run whose audio was unusable, before the timings,
  because it changes how the transcript above should be read.
- ADR 0015's and ADR 0016's open sentence now has a cause, a measurement, and a user-facing
  consequence rather than a note.

## Review trigger

When a device picker lands, or when a microphone on this hardware produces usable audio and
live recognition can finally be verified end to end.
