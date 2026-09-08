"""Whether the audio arriving is usable at all, and saying so when it is not.

A live translator whose input is clipped produces confident nonsense: the recogniser is
handed a square wave, emits words that were never spoken, and the translation makes them
worse. Nothing in the pipeline notices, because every stage does its job correctly on the
input it was given. The user sees fluent output and no reason to distrust it.

**This is not hypothetical.** ADR 0015 recorded "the microphone produces saturated audio"
and left it there, and the reference machine's mixer really does hold +60 dB of gain on its
internal microphone — a setting the user can fix in seconds *if something tells them to*,
and nothing did.

The measurement that number was taken from has since been corrected (ADR 0020): the 51% of
full-scale samples ADR 0019 cites came from the analog input powering up, not from the
microphone, and a `SettlingSource` now discards that.

**Clipping turned out to be the wrong thing to key on, on its own** (ADR 0021). Recorded
speech amplified until a fifth of its samples sit at full scale still transcribes word for
word; what actually produces invented words is an amplified *room*, where there is no pause
between anything because there is nothing being said. Those two have the same peak, the same
rms and the same crest factor, so no instantaneous measure separates them. What does is
whether the input ever goes quiet — see `TOO_LOUD` below.

So this module computes three numbers over a short window and turns them into one verdict.
It holds **no audio** — the readings are counts and ratios, `OPERATIONAL_METADATA` in the
sense `docs/RETENTION_POLICY.md` uses, and safe to display or log where a frame would not
be.

The thresholds are judgement calls, and they were calibrated rather than invented:

```text
                       peak    rms      clipped samples
recorded speech (en)   0.535   0.0471   0.0000%
recorded speech (ru)   0.500   0.0790   0.0000%
this machine's mic     1.000   0.8134   51.04%   <- the power-up, not the mic (ADR 0020)
```

The gap between working and clipped is three orders of magnitude, so the threshold sits
far from both, at **5% of samples in the window**. It has to clear transients as well as
speech: a door slam or a knock on the desk legitimately puts one whole frame at
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
#
# One count away from `formats.INT16_NORMALISATION_SCALE`, and deliberately: that one maps
# the type's range into [-1, 1) for a model, this one reports a peak as a fraction of the
# loudest representable sample. The comment beside it explains why neither should be
# tidied into the other.
FULL_SCALE = 32767.0
CLIPPED_SAMPLE = 32700

# Fractions of the window, not of a frame. One frame catching a door slam is not a verdict:
# a single fully-clipped 20 ms frame is 2% of a one-second window, so the threshold sits
# above that and far below the 51% a genuinely over-driven input produces.
CLIPPING_FRACTION = 0.05
SILENT_PEAK = 0.002
QUIET_RMS = 0.005

# The quietest tenth of a several-second window. Speech has pauses — between words, between
# sentences, between turns — so this tracks the room rather than the talking, and it is the
# one statistic that separates an over-driven microphone from an over-driven speaker.
#
# Measured, with the same recorded speech ADR 0019 calibrated against, amplified digitally:
#
#     speech x8   0.042      speech x24  0.122   (0.0% word error)
#     speech x12  0.063      speech x32  0.156   (5.6% word error)
#     a room at +60 dB       0.333
#     a room at +30 dB       0.009
#
# 0.15 sits between the loudest gain that still transcribes perfectly and the first that does
# not, and a factor of 2.2 below the input that invented words. It is placed where
# recognition measurably starts to fail rather than where the numbers look tidy.
LOUD_FLOOR = 0.15

# Five seconds. Long enough to contain a pause: at one second, heavily amplified speech and
# an amplified room are indistinguishable by this measure (0.45 against 0.43), and at five
# they are not (0.12 against 0.33). The cost is that this verdict cannot appear until five
# seconds of audio has been heard, which is the right trade for a warning that accuses a
# user's microphone.
FLOOR_WINDOW_FRAMES = 250

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
    TOO_LOUD = "too_loud"

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
        if self is InputQuality.TOO_LOUD:
            return (
                "the microphone is far too loud — turn its input gain down, or the room "
                "itself is transcribed as words nobody said"
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
    # The quietest tenth of the floor window, or None before enough audio has been heard to
    # say. Defaulted so that constructing a reading from three numbers still works.
    floor: float | None = None

    def __str__(self) -> str:
        floor = f", floor {self.floor:.3f}" if self.floor is not None else ""
        return (
            f"{self.quality} (peak {self.peak:.2f}, rms {self.rms:.3f}, "
            f"clipped {self.clipped_fraction:.1%}{floor})"
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
        "_floor_frames",
        "_floor_window",
        "_frames",
        "_total_clipped",
        "_total_peak",
        "_total_samples",
        "_total_squares",
        "_window",
        "_worst_floor",
    )

    def __init__(
        self,
        *,
        window_frames: int = DEFAULT_WINDOW_FRAMES,
        floor_window_frames: int = FLOOR_WINDOW_FRAMES,
    ) -> None:
        if window_frames < 1:
            raise ValueError("the window must cover at least one frame")
        if floor_window_frames < 1:
            raise ValueError("the floor window must cover at least one frame")
        self._window = window_frames
        self._frames: deque[tuple[float, float, int, int]] = deque(maxlen=window_frames)
        # A separate, longer window holding one number per frame. `TOO_LOUD` is a statement
        # about several seconds — whether the input ever goes quiet — and cannot be read off
        # the one-second window the other verdicts use.
        self._floor_window = floor_window_frames
        self._floor_frames: deque[float] = deque(maxlen=floor_window_frames)
        self._worst_floor: float | None = None
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
        self._floor_frames.clear()
        self._worst_floor = None
        self._total_peak = 0.0
        self._total_squares = 0.0
        self._total_clipped = 0
        self._total_samples = 0

    def observe(self, frame: bytes) -> LevelReading:
        """Add a frame and return the verdict for the window it now ends."""
        entry = frame_levels(frame)
        self._frames.append(entry)
        peak, squares, clipped, samples = entry
        if samples:
            self._floor_frames.append(math.sqrt(squares / samples) / FULL_SCALE)
            floor = self._floor
            if floor is not None:
                # The worst window seen, not the latest: a recording is judged on the
                # loudest stretch of room it contains, the same reasoning that gave
                # `overall` its running totals rather than a rolling verdict.
                self._worst_floor = (
                    floor if self._worst_floor is None else max(self._worst_floor, floor)
                )
        self._total_peak = max(self._total_peak, peak)
        self._total_squares += squares
        self._total_clipped += clipped
        self._total_samples += samples
        return self.reading

    @property
    def _floor(self) -> float | None:
        """The quietest tenth of the floor window, once there is a full window of it.

        `None` until then, deliberately: a verdict that tells someone their microphone is
        unusable should not be reached from two seconds of audio, and the separation this
        relies on only appears over several (ADR 0021).
        """
        if len(self._floor_frames) < self._floor_window:
            return None
        ordered = sorted(self._floor_frames)
        return ordered[int(0.10 * len(ordered))]

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
        floor = self._floor
        return LevelReading(peak, rms, clipped, _classify(peak, rms, clipped, floor), floor)

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
        # The worst window rather than the last one, for the same reason the totals exist,
        # and `None` when no window was ever long enough to compute one. It used to be
        # `self._worst_floor if self._floor_frames else None`, which returned the initial
        # 0.0 for any recording shorter than the floor window — printing `floor 0.000` for
        # a number nobody had measured, in the line that tells a user whether their
        # microphone is usable. It never misclassified, because 0.0 is below every
        # threshold; it stated a measurement that had not been taken, which this project
        # treats as the worse fault (ADR 0026).
        floor = self._worst_floor
        return LevelReading(
            self._total_peak,
            rms,
            clipped,
            _classify(self._total_peak, rms, clipped, floor),
            floor,
        )


def _classify(
    peak: float, rms: float, clipped_fraction: float, floor: float | None = None
) -> InputQuality:
    """Clipping first, then a floor that never drops.

    Both produce fluent words nobody said, which is the failure a user cannot see for
    themselves; a silent or quiet input yields no transcript or an obviously poor one, which
    they can. Clipping is reported ahead of `TOO_LOUD` because it is the more specific
    statement about the same problem, and because it can be said from one second of audio
    rather than five.
    """
    if clipped_fraction >= CLIPPING_FRACTION:
        return InputQuality.CLIPPING
    if floor is not None and floor >= LOUD_FLOOR:
        return InputQuality.TOO_LOUD
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
