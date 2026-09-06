#!/usr/bin/env python3
"""Measure the pauses in live speech, to calibrate endpointing against.

```bash
python scripts/measure_pauses.py
```

It listens, waits for the first word, and then reports for thirty seconds how long the
silences between speech last — and, for each candidate threshold, how many of them would end
an utterance. That last column is the one that matters:
`SILENCE_AFTER_SPEECH_SECONDS` is only meaningful as "how often does this fire on a real
person", and no recording in this project can answer it, because published speech clips are
trimmed and contain no natural pauses (ADR 0024).

**It keeps no audio.** Frames are measured and dropped; what is retained is a list of
durations and a per-second summary of whether anyone was talking. Those are
`OPERATIONAL_METADATA` under `docs/RETENTION_POLICY.md`, and this script exists partly to
make that the easy thing to do — the alternative, recording someone and analysing the file,
would retain speech in order to tune a parameter.

**It waits for the speaker rather than for a clock.** Every earlier run of this measurement
was corrupted by the gap between starting it and the speaker learning it had started; one
measured 26 seconds of an empty room and 2 of speech. The window opens on the first word.

**The threshold is fixed, not adaptive.** `EnergyVoiceActivityDetector` seeds its noise floor
from its first frame, so an instrument armed while somebody is already talking seeds on
speech and goes deaf — measured, 75.5% of frames detected against 5.2% on identical audio.
An instrument must not share a defect with the thing it is measuring.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from on_the_fly.domain.audio.settling import SettlingSource
from on_the_fly.domain.audio.vad import frame_rms
from on_the_fly.infrastructure.audio import MicrophoneSource

FRAME_MS = 20

# Absolute rms on the 0-32767 scale, between a measured room floor near 500 and this
# speaker's speech near 2400. Well above the domain's 120 "absolute silence" constant, since
# this has to separate speech from a room rather than sound from nothing.
SPEECH_RMS = 1200.0

# 100 ms above the threshold before the window opens, so a cough or a keystroke does not arm
# it and steal the first seconds of the measurement.
ARM_FRAMES = 5

CANDIDATES = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=30.0, help="how long to measure")
    parser.add_argument("--wait", type=float, default=240.0, help="how long to wait for a voice")
    parser.add_argument("--device", default=None, help="input device index or name substring")
    args = parser.parse_args(argv)

    device: int | str | None = None
    if args.device is not None:
        device = int(args.device) if args.device.lstrip("-").isdigit() else args.device

    source = SettlingSource(MicrophoneSource(frame_ms=FRAME_MS, device=device))
    gaps: list[float] = []
    timeline: list[str] = []
    bucket: list[float] = []
    silence_run = 0
    heard_speech = False
    speech_frames = 0
    loudest = 0.0
    consecutive = 0
    armed = False
    waited = 0.0

    print(f"listening - start speaking whenever you like (waiting up to {args.wait:.0f}s)")
    opened = time.monotonic()
    started = opened
    try:
        for frame in source.frames():
            rms = frame_rms(frame)

            if not armed:
                consecutive = consecutive + 1 if rms > SPEECH_RMS else 0
                if consecutive >= ARM_FRAMES:
                    armed = True
                    waited = time.monotonic() - opened
                    started = time.monotonic()
                elif time.monotonic() - opened >= args.wait:
                    break
                continue

            loudest = max(loudest, rms)
            bucket.append(rms)
            if len(bucket) >= 1000 // FRAME_MS:
                talking = sum(1 for value in bucket if value > SPEECH_RMS)
                timeline.append("#" if talking > len(bucket) // 2 else "-" if talking else ".")
                bucket = []

            if rms > SPEECH_RMS:
                speech_frames += 1
                if silence_run and heard_speech:
                    gaps.append(silence_run * FRAME_MS / 1000)
                silence_run = 0
                heard_speech = True
            else:
                silence_run += 1

            if time.monotonic() - started >= args.seconds:
                break
    finally:
        source.close()

    if not armed:
        print(f"no speech in {args.wait:.0f}s - nothing was said, or nothing arrived")
        return 1

    measured = time.monotonic() - started
    print(f"\narmed after {waited:.1f}s, measured {measured:.1f}s")
    print(f"speech in {speech_frames * FRAME_MS / 1000:.1f}s of {measured:.1f}s")
    print(f"loudest {loudest:.0f} rms (threshold {SPEECH_RMS:.0f})")
    print(f"per second: {''.join(timeline)}    (# talking, - partly, . quiet)\n")

    if not gaps:
        print("no pauses between speech - nothing to calibrate against")
        return 1

    gaps.sort()

    def percentile(fraction: float) -> float:
        return gaps[min(len(gaps) - 1, int(fraction * len(gaps)))]

    print(
        f"{len(gaps)} pauses:  shortest {gaps[0]:.2f}s   p50 {percentile(0.50):.2f}s   "
        f"p75 {percentile(0.75):.2f}s   p90 {percentile(0.90):.2f}s   longest {gaps[-1]:.2f}s\n"
    )
    print("  threshold -> pauses it would end an utterance on")
    for threshold in CANDIDATES:
        firing = sum(1 for gap in gaps if gap >= threshold)
        cadence = f"one utterance per {measured / firing:.1f}s" if firing else "never fires"
        share = firing / len(gaps)
        print(f"    {threshold:.1f}s   {firing:>3} of {len(gaps)} ({share:>5.1%})   {cadence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
