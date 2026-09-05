"""Whether the audio arriving is usable at all, and saying so when it is not.

A live translator whose input is clipped produces confident nonsense: the recogniser is
handed a square wave, emits words that were never spoken, and the translation makes them
worse. Nothing in the pipeline notices, because every stage does its job correctly on the
input it was given. The user sees fluent output and no reason to distrust it.

**This is not hypothetical.** The reference machine's capture path measures a peak of 1.0
and **51% of samples at full scale**, against 0.5 peak and 0.0% for the recorded speech this
project tests with. ADR 0015 recorded that as "the microphone produces saturated audio" and
left it there; the cause is the capture gain pinned at +30 dB in the system mixer, which is
a setting the user can fix in seconds *if something tells them to*. Nothing did.

So this module computes three numbers over a short window and turns them into one verdict.
It holds **no audio** — the readings are counts and ratios, `OPERATIONAL_METADATA` in the
sense `docs/RETENTION_POLICY.md` uses, and safe to display or log where a frame would not
be.

The thresholds are judgement calls, and they were calibrated rather than invented:

```text
                       peak    rms      clipped samples
recorded speech (en)   0.535   0.0471   0.0000%
recorded speech (ru)   0.500   0.0790   0.0000%
this machine's mic     1.000   0.8134   51.04%
```

The gap between working and broken is three orders of magnitude in clipping, so the
threshold sits far from both, at **5% of samples in the window**. It has to clear transients
as well as speech: a door slam or a knock on the desk legitimately puts one whole frame at
the rails, which is 2% of a one-second window, and a warning that flickers on every loud
noise is a warning people learn to ignore. Sustained clipping across a twentieth of a second
of every second is not a transient.
"""

from __future__ import annotations

import math
from array import array
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.ports import AudioSource

# 16-bit signed PCM. Full scale is 32767; a sample at or beyond this counts as clipped —
# not exactly 32767, because resampling and dithering shave a count or two off a saturated
# signal without making it less saturated.
FULL_SCALE = 32767.0
CLIPPED_SAMPLE = 32700

# Fractions of the window, not of a frame. One frame catching a door slam is not a verdict:
# a single fully-clipped 20 ms frame is 2% of a one-second window, so the threshold sits
# above that and far below the 51% a genuinely over-driven input produces.
CLIPPING_FRACTION = 0.05
SILENT_PEAK = 0.002
QUIET_RMS = 0.005

# One second at 20 ms frames. Long enough that a single loud syllable does not condemn a
# device, short enough that a user who fixes their gain sees the warning clear while they
# are still looking at it.
DEFAULT_WINDOW_FRAMES = 50


class InputQuality(Enum):
    """The verdict, in the terms a user can act on rather than the ones a meter uses."""

    OK = "ok"
    SILENT = "silent"
    QUIET = "quiet"
    CLIPPING = "clipping"

    def __str__(self) -> str:
        return self.value

    @property
    def is_usable(self) -> bool:
        """Whether speech in this audio has a reasonable chance of being recognised."""
        return self is InputQuality.OK

    @property
    def advice(self) -> str:
        """What the user should do about it. Empty when there is nothing to do.

        Phrased as an instruction to the person, not a description of the signal: "input is
        clipping" is a fact about the audio and useless to someone who does not know what
        clipping is.
        """
        if self is InputQuality.CLIPPING:
            return (
                "the microphone is too loud and the audio is distorting — turn its input gain down"
            )
        if self is InputQuality.SILENT:
            return "no sound is arriving — the microphone may be muted or the wrong device"
        if self is InputQuality.QUIET:
            return "the microphone is very quiet — turn its input gain up or move closer"
        return ""


@dataclass(frozen=True)
class LevelReading:
    """What the last window of audio looked like. Numbers only; never audio."""

    peak: float
    rms: float
    clipped_fraction: float
    quality: InputQuality

    def __str__(self) -> str:
        return (
            f"{self.quality} (peak {self.peak:.2f}, rms {self.rms:.3f}, "
            f"clipped {self.clipped_fraction:.1%})"
        )


def frame_levels(frame: bytes) -> tuple[float, float, int, int]:
    """`(peak, sum of squares, clipped samples, sample count)` for one PCM frame.

    Returns the sum rather than the mean so a window can be aggregated exactly, instead of
    averaging averages over frames that may differ in length.
    """
    if not frame:
        return (0.0, 0.0, 0, 0)
    if len(frame) % 2 != 0:
        raise ValueError(f"frame of {len(frame)} bytes is not a whole number of 16-bit samples")
    samples = array("h")
    samples.frombytes(frame)
    peak = 0
    clipped = 0
    total = 0.0
    for sample in samples:
        magnitude = -sample if sample < 0 else sample
        if magnitude > peak:
            peak = magnitude
        if magnitude >= CLIPPED_SAMPLE:
            clipped += 1
        total += float(sample) * float(sample)
    return (peak / FULL_SCALE, total, clipped, len(samples))


class LevelMonitor:
    """Rolling verdict on the input, over the last `window_frames` frames.

    Holds four counters per frame and nothing else. There is no path from this object back
    to the audio it observed, which is deliberate: it exists to be safe to keep around and
    to display, in a pipeline where nothing else is.
    """

    __slots__ = (
        "_frames",
        "_total_clipped",
        "_total_peak",
        "_total_samples",
        "_total_squares",
        "_window",
    )

    def __init__(self, *, window_frames: int = DEFAULT_WINDOW_FRAMES) -> None:
        if window_frames < 1:
            raise ValueError("the window must cover at least one frame")
        self._window = window_frames
        self._frames: deque[tuple[float, float, int, int]] = deque(maxlen=window_frames)
        # Running totals as well as the window. The window is what a live caption needs —
        # "is the microphone bad *now*" — and totals are what a finished recording needs,
        # because the last second of a file is usually its silent tail and a verdict taken
        # there describes nothing that was said.
        self._total_peak = 0.0
        self._total_squares = 0.0
        self._total_clipped = 0
        self._total_samples = 0

    @property
    def window_frames(self) -> int:
        return self._window

    def reset(self) -> None:
        self._frames.clear()
        self._total_peak = 0.0
        self._total_squares = 0.0
        self._total_clipped = 0
        self._total_samples = 0

    def observe(self, frame: bytes) -> LevelReading:
        """Add a frame and return the verdict for the window it now ends."""
        entry = frame_levels(frame)
        self._frames.append(entry)
        peak, squares, clipped, samples = entry
        self._total_peak = max(self._total_peak, peak)
        self._total_squares += squares
        self._total_clipped += clipped
        self._total_samples += samples
        return self.reading

    @property
    def reading(self) -> LevelReading:
        samples = sum(entry[3] for entry in self._frames)
        if not samples:
            # No audio seen yet is not a complaint about the device. Reporting SILENT here
            # would put a warning on screen before the first frame arrives.
            return LevelReading(0.0, 0.0, 0.0, InputQuality.OK)

        peak = max(entry[0] for entry in self._frames)
        rms = math.sqrt(sum(entry[1] for entry in self._frames) / samples) / FULL_SCALE
        clipped = sum(entry[2] for entry in self._frames) / samples
        return LevelReading(peak, rms, clipped, _classify(peak, rms, clipped))

    @property
    def overall(self) -> LevelReading:
        """The verdict over everything observed since the last reset.

        What a finished recording gets judged on. The rolling `reading` would report on its
        silent tail, which is why the first version of this reported nothing at all for a
        file that was clipped from end to end.
        """
        if not self._total_samples:
            return LevelReading(0.0, 0.0, 0.0, InputQuality.OK)
        rms = math.sqrt(self._total_squares / self._total_samples) / FULL_SCALE
        clipped = self._total_clipped / self._total_samples
        return LevelReading(
            self._total_peak, rms, clipped, _classify(self._total_peak, rms, clipped)
        )


def _classify(peak: float, rms: float, clipped_fraction: float) -> InputQuality:
    """Clipping first: it is the failure that produces confident nonsense rather than none.

    A silent or quiet input yields no transcript or an obviously poor one, which a user can
    see. Distortion yields fluent words nobody said, which they cannot.
    """
    if clipped_fraction >= CLIPPING_FRACTION:
        return InputQuality.CLIPPING
    if peak < SILENT_PEAK:
        return InputQuality.SILENT
    if rms < QUIET_RMS:
        return InputQuality.QUIET
    return InputQuality.OK


class LevelWatchingSource:
    """An `AudioSource` that measures what passes through it.

    A decorator rather than a change to the microphone adapter, for two reasons. The
    pipeline reads frames from a port and the monitor needs the same frames, so this is the
    one place both are true at once; and a WAV file gets the same check for free, which
    matters because a recording made on a badly set-up machine is clipped in exactly the
    same way a live capture is.

    It holds the latest `LevelReading` and no audio. Frames pass straight through — they are
    not copied, buffered, or retained, so this adds nothing to what
    `docs/RETENTION_POLICY.md` has to account for.
    """

    __slots__ = ("_monitor", "_source")

    def __init__(self, source: AudioSource, *, monitor: LevelMonitor | None = None) -> None:
        self._source = source
        self._monitor = monitor if monitor is not None else LevelMonitor()

    @property
    def audio_format(self) -> AudioFormat:
        return self._source.audio_format

    @property
    def level(self) -> LevelReading:
        """The current verdict, over the last second. Safe to read from another thread."""
        return self._monitor.reading

    @property
    def overall_level(self) -> LevelReading:
        """The verdict over everything that has passed through, for a finished run."""
        return self._monitor.overall

    @property
    def source(self) -> AudioSource:
        """The wrapped source, for callers needing something this port does not expose."""
        return self._source

    def frames(self) -> Iterator[bytes]:
        for frame in self._source.frames():
            self._monitor.observe(frame)
            yield frame

    def close(self) -> None:
        self._source.close()
