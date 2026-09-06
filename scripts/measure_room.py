#!/usr/bin/env python3
"""Measure the room's reverberation with a swept sine, to place it on ADR 0027's grid.

```bash
python scripts/measure_room.py            # plays a sweep out loud, four times
python scripts/measure_room.py --amplitude 0.3 --repeats 2
```

**It makes a loud noise.** Four six-second sweeps through the speakers, recorded on the
microphone and deconvolved with the inverse filter (Farina's method). Averaging coherent
sweeps lifts the result out of the room's noise: the sweep adds in phase every time, the
noise does not.

What it retains is an impulse response and the numbers derived from it. The recording is of a
test signal, not of anybody, and nothing is written unless `--save` is given.

**Read the caveat before trusting the number.** On a laptop the speaker and the microphone
are a hand's width apart on the same chassis, so the direct path dominates — measured here,
91.5% of the energy arrives within 5 ms — and only what is left carries information about the
room. The reverberation *time* survives that, because RT60 is a property of the room rather
than of where you stand in it. The direct-to-reverberant ratio does not, and is reported only
to show how unlike a talking person this geometry is (ADR 0029).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

RATE = 16_000
SWEEP_LOW_HZ = 60.0
SWEEP_HIGH_HZ = 7_600.0
SWEEP_SECONDS = 6.0
TAIL_SECONDS = 2.0

# The Schroeder curve is only worth fitting where it is still decaying. Past the measurement
# noise floor it flattens, and a fit that includes the flat part reports the noise rather than
# the room — which is how a first attempt here produced 1.3 s for a response with 99% of its
# energy in the first 100 ms.
FIT_FROM_MS = 20
FIT_TO_MS = 120


def sweep_and_inverse(seconds: float) -> tuple[np.ndarray, np.ndarray]:
    count = int(RATE * seconds)
    t = np.arange(count) / RATE
    ratio = np.log(SWEEP_HIGH_HZ / SWEEP_LOW_HZ)
    sweep = np.sin(2 * np.pi * SWEEP_LOW_HZ * seconds / ratio * (np.exp(t * ratio / seconds) - 1.0))
    fade = int(RATE * 0.05)
    sweep[:fade] *= np.linspace(0.0, 1.0, fade)
    sweep[-fade:] *= np.linspace(1.0, 0.0, fade)
    inverse = sweep[::-1] * np.exp(-t * ratio / seconds)
    return sweep, inverse


def reverberation_time(rir: np.ndarray) -> tuple[float, float]:
    """`(RT60, decibels of usable decay)` from the Schroeder integral."""
    peak = int(np.argmax(np.abs(rir)))
    tail = rir[peak:]
    energy = np.cumsum(tail[::-1] ** 2)[::-1]
    if energy[0] <= 0:
        return float("nan"), 0.0
    db = 10 * np.log10(np.maximum(energy / energy[0], 1e-12))
    seconds = np.arange(len(db)) / RATE
    start, end = int(RATE * FIT_FROM_MS / 1000), int(RATE * FIT_TO_MS / 1000)
    if end >= len(db):
        return float("nan"), 0.0
    slope = float(np.polyfit(seconds[start:end], db[start:end], 1)[0])
    return (-60.0 / slope if slope < 0 else float("nan")), float(db[start] - db[end])


def early_energy_share(rir: np.ndarray, milliseconds: float) -> float:
    peak = int(np.argmax(np.abs(rir)))
    count = int(RATE * milliseconds / 1000)
    total = float(np.sum(rir[peak:] ** 2))
    return float(np.sum(rir[peak : peak + count] ** 2)) / total if total else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--amplitude", type=float, default=0.5, help="playback level, 0-1")
    parser.add_argument("--repeats", type=int, default=4, help="sweeps to average")
    parser.add_argument("--save", type=Path, default=None, help="write the response as .npy")
    args = parser.parse_args(argv)

    try:
        import sounddevice
    except ImportError:
        print("this needs sounddevice, which arrives with the capture stack")
        return 1

    sweep, inverse = sweep_and_inverse(SWEEP_SECONDS)
    played = np.concatenate([sweep, np.zeros(int(RATE * TAIL_SECONDS))]) * args.amplitude

    print(f"playing {args.repeats} x {SWEEP_SECONDS:.0f}s sweep - this is audible")
    captures = []
    for index in range(args.repeats):
        recorded = sounddevice.playrec(played, samplerate=RATE, channels=1, dtype="float32")
        sounddevice.wait()
        captured = recorded[:, 0].astype(np.float64)
        peak = float(np.max(np.abs(captured)))
        note = "  CLIPPING - lower --amplitude" if peak >= 0.99 else ""
        print(f"  sweep {index + 1}: peak {peak:.3f}{note}")
        if peak < 0.02:
            print("recorded almost nothing - is playback muted, or the wrong device?")
            return 1
        captures.append(captured)

    deconvolved = np.convolve(np.mean(captures, axis=0), inverse)
    start = int(np.argmax(np.abs(deconvolved)))
    rir = deconvolved[start : start + int(RATE * 1.5)]
    rir = rir / np.max(np.abs(rir))

    rt60, usable = reverberation_time(rir)
    print(f"\nRT60 {rt60:.2f}s, fitted over {FIT_FROM_MS}-{FIT_TO_MS}ms ({usable:.1f} dB of decay)")
    for milliseconds in (5, 20, 50, 100):
        print(
            f"  energy within {milliseconds:>3}ms of the peak: "
            f"{early_energy_share(rir, milliseconds):.1%}"
        )
    print("\nThe direct path dominates on built-in hardware; see this script's docstring.")

    if args.save is not None:
        np.save(args.save, rir)
        print(f"response written to {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
