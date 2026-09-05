# ADR 0015 — Capture through the audio callback, not blocking reads

**Status:** Accepted
**Date:** 2026-09-04
**Deciders:** @tehki
**Risk:** MODERATE — rewrites how the only privileged device this project opens delivers audio
**Supersedes in part:** [ADR 0013](0013-capture-rate-negotiation.md), which recorded this crash as out of scope

## Context

ADR 0013 recorded a defect and deliberately left it:

> `Built-in Audio Analog Stereo` — a 4-channel device opened as mono — aborts the process
> during blocking reads: `malloc(): unsorted double linked list corrupted`. **This is not
> our code.** It reproduces with raw `sounddevice.RawInputStream` and a blocking `read()`
> loop, with no part of this project involved.

That was true and it was not a reason to stop. A hard crash cannot be caught: `SIGABRT`
takes the process down, and a desktop translator a user can kill by choosing the wrong
entry in a device list is not shippable. The requirement is to run on any hardware, which
means one device in three aborting the application is a blocker rather than a footnote.

## The mechanism

The diagnosis in ADR 0013 contained the fix without naming it. `sounddevice.rec()` worked on
the same device at the same rate while blocking `read()` crashed — and `rec()` uses
PortAudio's **callback** API.

Verified directly: a `RawInputStream` opened with a `callback` on the crashing device
delivers audio and closes cleanly. The blocking read path is the one that corrupts the heap.

## Decision

**Capture through the callback API.** PortAudio delivers blocks on its own audio thread; the
callback copies them into a bounded queue; `read()` takes from that queue.

The `InputStream` port is unchanged, so nothing above the backend knows. `MicrophoneSource`,
the resampler, the pipeline and the CLI are untouched.

### The callback does almost nothing

It runs on a high-priority audio thread. Copy the bytes, put them in the queue, return.
No parsing, no logging, no lock another thread might hold — anything slower there becomes a
drop-out in the captured audio itself.

### The queue is bounded, and drops the oldest

Two seconds at 20 ms blocks. When the consumer falls behind, the **oldest** block is
discarded to make room for the newest, and the drop is counted.

Both halves of that matter. Unbounded buffering would turn a drop-out into a memory leak and
feed the recogniser audio that is seconds stale — for a live translator, late speech is worse
than missing speech, because the user cannot tell it is late. Dropping the newest instead
would discard the thing the speaker just said.

### Silence is a failure, not a quiet room

`read()` waits up to five seconds. The callback delivers blocks whether or not anyone is
speaking, so five seconds of nothing from a running stream means the device stopped — most
often unplugged mid-sentence. It becomes a typed `AudioDeviceError` rather than a hang.

That is a real behavioural improvement over blocking reads, which had no way to distinguish
a disconnected device from a patient one.

## Verified on the hardware that used to crash

| Device | Before | After |
| --- | --- | --- |
| `Built-in Analog` (4-channel, 48 kHz only) | **`SIGABRT`, core dumped** | 48000 Hz, resampled, 50 frames, 0 overflows |
| `HDA Intel PCH analog` | 44100 Hz, resampled | 44100 Hz, resampled, 0 overflows |
| `default` (mixer) | 16000 Hz, direct | 16000 Hz, direct, 0 overflows |

All three now capture. None aborts.

## Consequences

- **Capture works on every input device on the reference machine**, which was the point.
- A disconnected device is now a typed error instead of an indefinite wait.
- Overflow counting is more honest: it reports both PortAudio's own overflow flag and blocks
  this project dropped, where before it could only report the former.
- Queued audio is discarded on close. Captured audio is `EPHEMERAL` and has no reason to
  outlive the device that produced it.
- The audio thread is a new concurrency boundary. It touches two integers and a bounded
  queue, and nothing else in the process shares them.

## What this does not claim

The underlying crash is still a defect in `sounddevice`'s blocking-read path, not something
this project fixed. This routes around it. If a future device crashes the *callback* path
too, the answer would be a capture subprocess — isolating the native library behind a process
boundary so its aborts cost a restart rather than the application — which is a larger change
and unnecessary while this one works.

The audio on the reference machine is still saturated and DC-offset, so **recognition from a
live microphone remains unverified**. This ADR is about not crashing, not about hearing.

> **Followed up 2026-09-05 in [ADR 0019](0019-input-levels.md).** The saturation has a cause:
> the system mixer's capture gain is pinned at +30 dB, which drives the room's noise floor
> into the rails — 51% of samples at full scale, against 0.0% for recorded speech. It is not
> the device selection (checked) and not this adapter. The application now measures the input
> and tells the user what to change instead of transcribing the noise into confident nonsense.
> Live recognition is still unverified, because that needs a mixer change on someone's
> machine rather than a code change.

## Review trigger

If any device aborts the process again, or before phase 2 — mobile audio stacks are not
PortAudio, and the callback-versus-blocking question will be a different question there.
