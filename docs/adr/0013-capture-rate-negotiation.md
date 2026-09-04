# ADR 0013 — Negotiate the capture rate, and resample to 16 kHz

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — changes what the microphone adapter asks hardware for, on the only privileged device this project opens

## Context

The pipeline works in 16 kHz mono 16-bit PCM, because that is what the recognition models
take. `MicrophoneSource` therefore asked the device for 16 kHz, and if the device said no,
capture failed.

Running it against real hardware for the first time showed that devices say no. Measured on
the reference machine:

| Device | Native | 16 kHz | 44.1 kHz | 48 kHz |
| --- | --- | --- | --- | --- |
| `HDA Intel PCH: ALC269VC Analog` | 44100 | **refused** | ok | ok |
| `Built-in Audio Analog Stereo` | 48000 | **refused** | refused | ok |
| `default` (the system mixer) | 44100 | ok | ok | ok |

Only the software mixer accepts 16 kHz, and it accepts it by resampling internally. **Every
actual hardware device refused**, with:

```text
AudioDeviceError: could not open an input stream at 16000Hz: Error opening Raw
```

So the adapter worked only when pointed at a resampling layer that happened to be the
default. Anyone selecting a real device — which a device picker exists to let them do — got
a failure. That is not a rare configuration; 44.1 and 48 kHz are what audio hardware does.

## Decision

**Negotiate the rate with the device, then resample to 16 kHz in the adapter.**

1. Ask for 16 kHz first. When the device supports it, nothing changes and no resampling
   happens.
2. If the device refuses, open at a rate it accepts — its native rate first, then a short
   preference list — and resample the captured audio to 16 kHz before it leaves the adapter.
3. The `AudioFormat` the rest of the application sees is unchanged. Resampling is an
   adapter concern and the domain never learns the device disagreed.

The pipeline continues to receive exactly what it received before: 16 kHz mono int16 frames
of a fixed size. Only the edge changed.

## Why resample here rather than let the mixer do it

The mixer *does* resample, which is why `default` works — and relying on that would mean
the application only functions through one particular device entry, silently, with the
resampling quality and buffering of whatever sound server is installed. Making it explicit
means it is visible, testable, and the same on every platform, which matters for a project
whose phase 2 is mobile (ADR 0002).

## Why not write the resampler

Downsampling without an anti-aliasing filter folds high-frequency content back into the
speech band. It does not raise an error; it degrades recognition quietly, and this project
has already been caught once by a seam that failed silently rather than loudly (ADR 0009's
uppercase transcripts). 48 kHz to 16 kHz is a clean 3:1 decimation, but 44.1 kHz to 16 kHz
is 160:441 — a rational resampler with a designed filter, which is real DSP.

Handbook guidance is to prefer established mechanisms over inventing one, and audio
resampling is exactly the case it describes. `av` wraps libswresample, which is the
resampler FFmpeg uses.

## Admission review — `av` (Article 12)

| Criterion | Finding |
| --- | --- |
| Licence | **BSD-3-Clause** |
| Need | Concrete: correct rational resampling for rates real hardware offers |
| Footprint | **Already installed and already required** — `faster-whisper` depends on `av>=11`. No transitive dependencies of its own |
| Install scripts | None. Wheel, with FFmpeg libraries bundled |
| Provenance | PyAV, the long-standing FFmpeg binding for Python |
| Alternatives | `scipy.signal.resample_poly` (a much larger new dependency); `soxr` (new); hand-written (rejected above) |

**It is promoted from transitive to declared**, the same decision and for the same reason as
`ctranslate2` in ADR 0009: a transitive dependency is another package's choice, and this
project now depends on it directly.

One honest note on footprint: `av` bundles prebuilt FFmpeg binaries, so it carries the same
kind of exposure ADR 0003 recorded for PortAudio — a large native surface arriving as a
wheel. It is not new exposure, since the wheel is already installed, but declaring it makes
the project's reliance on it explicit rather than incidental.

## Probe, never retry — learned the hard way

The first implementation tried each candidate rate by **opening** a stream and catching the
failure. That crashed:

```text
malloc(): mismatching next->prev_size (unsorted)
Aborted (core dumped)
```

Four failed `open_input_stream` calls in one process corrupt the heap in PortAudio's ALSA
backend. Not an exception — a `SIGABRT` that takes the whole application down, which no
`except` can catch.

So negotiation **probes** with `check_input_settings`, which does not open a stream, and
then opens exactly once. Twelve probes across three devices run cleanly; four failed opens
do not. If every probe says no, one honest open is still attempted — a backend whose probe
is unreliable deserves an attempt — but only one.

The tests assert this directly: `test_a_device_supporting_nothing_still_gets_exactly_one_open_attempt`
fails if a retry is ever reintroduced.

## Verified on real hardware

| Device | Offers | Negotiated | Result |
| --- | --- | --- | --- |
| `default` (mixer) | 16 kHz and everything | 16000 | 50 frames, no resampling, 0 overflows |
| `HDA Intel PCH analog` | 44.1/48 kHz, **refuses 16 kHz** | 44100 | **50 frames, resampled**, 1 overflow |
| `Built-in Analog` | 48 kHz only | — | **crashes the process — see below** |

The middle row is the point of this ADR: a device that previously failed outright now
captures, and the frames reaching the pipeline are the same 640 bytes they always were.

## An unrelated crash this uncovered

`Built-in Audio Analog Stereo` — a 4-channel device opened as mono — aborts the process
during blocking reads:

```text
malloc(): unsorted double linked list corrupted
```

**This is not our code.** It reproduces with raw `sounddevice.RawInputStream` and a blocking
`read()` loop, with no part of this project involved, while `sounddevice.rec()` on the same
device at the same rate works fine. So it is a defect in that library's blocking-read path
on this device, not in the adapter.

It matters anyway, and it is recorded rather than filed away: **a hard crash cannot be
caught**. A desktop translator that a user can kill by selecting the wrong entry in a device
list is not acceptable, and no amount of exception handling in this adapter fixes it. The
options are to capture in a separate process, or to use the callback API rather than
blocking reads. Both are design decisions and neither belongs in this change.

## Consequences

- **Capture works on hardware that offers only 44.1 or 48 kHz**, which is most of it.
- When the device gives 16 kHz, the resampler is never constructed and the path is
  byte-for-byte what it was.
- The adapter reports the rate actually negotiated. A sample rate is `OPERATIONAL_METADATA`
  — a number, not content — so it is safe to log, unlike the device name (ADR 0003).
- Resampling changes sample counts, so a fixed-size output frame needs a buffer. It is
  bounded: at most one input block plus one output frame is held, and it is discarded when
  the device closes. Audio that has been resampled is still `EPHEMERAL` and still never
  written anywhere by this adapter.
- A device that refuses **every** candidate rate still fails, and fails loudly with the
  rates that were tried. Silently succeeding at some other format would be worse.

## What this does not fix

**The captured audio on this machine is still unusable** — saturated and DC-offset,
confirmed identical through raw `sounddevice`, so it is the hardware rather than the
adapter. Rate negotiation removes one real obstacle between the pipeline and a working
microphone. It does not conjure a working microphone, and recognition from live capture
remains unverified.

**The blocking-read crash above is untouched.** One of three input devices on the reference
machine still aborts the process when captured from, for reasons outside this project.

## Review trigger

If a platform is added where the resampler is unavailable or the negotiation list is wrong
for its audio stack — mobile in phase 2 is the obvious candidate, since Android and iOS both
have their own capture-rate conventions.
