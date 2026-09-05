"""Tests for input level monitoring (ADR 0019).

The failure this guards against is the one the pipeline cannot notice: clipped audio makes
the recogniser emit words nobody said, and every stage downstream does its job correctly on
that input. So the tests are mostly about *classification being right at the boundaries*,
and about the monitor holding numbers rather than audio.

Frames are synthesised rather than recorded. The thresholds were calibrated against real
speech (`docs/adr/0019-input-levels.md`); what is asserted here is that the classifier says
what those numbers imply.
"""

from __future__ import annotations

import math
from array import array

import pytest

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.levels import (
    CLIPPED_SAMPLE,
    FULL_SCALE,
    InputQuality,
    LevelMonitor,
    LevelWatchingSource,
    frame_levels,
)

SAMPLES_PER_FRAME = 320  # 20 ms at 16 kHz


def tone(amplitude: float, samples: int = SAMPLES_PER_FRAME, *, period: int = 40) -> bytes:
    """A sine at `amplitude` of full scale, as 16-bit PCM."""
    values = array(
        "h",
        (
            int(
                max(
                    -FULL_SCALE,
                    min(FULL_SCALE, amplitude * FULL_SCALE * math.sin(2 * math.pi * i / period)),
                )
            )
            for i in range(samples)
        ),
    )
    return values.tobytes()


def square(samples: int = SAMPLES_PER_FRAME) -> bytes:
    """What a badly over-driven microphone actually delivers: everything at the rails."""
    values = array(
        "h", (CLIPPED_SAMPLE if i % 40 < 20 else -CLIPPED_SAMPLE for i in range(samples))
    )
    return values.tobytes()


def silence(samples: int = SAMPLES_PER_FRAME) -> bytes:
    return array("h", (0 for _ in range(samples))).tobytes()


def fill(monitor: LevelMonitor, frame: bytes) -> None:
    for _ in range(monitor.window_frames):
        monitor.observe(frame)


# --------------------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------------------


def test_clipped_input_is_reported() -> None:
    """The reference machine measures 51% of samples at full scale. This is that."""
    monitor = LevelMonitor()
    fill(monitor, square())

    reading = monitor.reading

    assert reading.quality is InputQuality.CLIPPING
    assert reading.clipped_fraction > 0.5
    assert reading.quality.is_usable is False


def test_ordinary_speech_levels_are_not_reported() -> None:
    """Recorded speech measures peak 0.5, rms 0.05, no clipping. A warning here is noise."""
    monitor = LevelMonitor()
    fill(monitor, tone(0.5))

    assert monitor.reading.quality is InputQuality.OK
    assert monitor.reading.quality.advice == ""


def test_digital_silence_is_reported_as_silent() -> None:
    monitor = LevelMonitor()
    fill(monitor, silence())

    assert monitor.reading.quality is InputQuality.SILENT


def test_audible_but_far_too_quiet_is_reported_as_quiet() -> None:
    monitor = LevelMonitor()
    fill(monitor, tone(0.004))

    assert monitor.reading.quality is InputQuality.QUIET


def test_clipping_outranks_everything_else() -> None:
    """It is the failure that produces fluent, wrong output rather than obviously poor output."""
    monitor = LevelMonitor()
    fill(monitor, silence())
    for _ in range(monitor.window_frames):
        monitor.observe(square())

    assert monitor.reading.quality is InputQuality.CLIPPING


def test_one_loud_frame_does_not_condemn_a_device() -> None:
    """A door slam is not a broken microphone. The verdict is over a window."""
    monitor = LevelMonitor()
    fill(monitor, tone(0.5))

    monitor.observe(square())

    assert monitor.reading.quality is InputQuality.OK


def test_a_fixed_microphone_stops_being_reported() -> None:
    """The user turns the gain down; the warning has to clear while they are looking at it."""
    monitor = LevelMonitor()
    fill(monitor, square())
    assert monitor.reading.quality is InputQuality.CLIPPING

    fill(monitor, tone(0.5))
    recovered = monitor.reading

    assert recovered.quality is InputQuality.OK


def test_a_recording_is_judged_on_all_of_it_not_its_tail() -> None:
    """The first version of this reported nothing for a file clipped end to end.

    A rolling window is right for a live caption and wrong for a finished recording, whose
    last second is usually silence — so the verdict there came from the silence.
    """
    monitor = LevelMonitor()
    fill(monitor, square())
    fill(monitor, silence())

    assert monitor.reading.quality is InputQuality.SILENT
    assert monitor.overall.quality is InputQuality.CLIPPING


def test_the_overall_verdict_starts_empty_and_resets() -> None:
    monitor = LevelMonitor()
    assert monitor.overall.quality is InputQuality.OK

    fill(monitor, square())
    monitor.reset()

    assert monitor.overall.quality is InputQuality.OK
    assert monitor.overall.clipped_fraction == 0.0


def test_nothing_seen_yet_is_not_a_complaint() -> None:
    """A warning before the first frame arrives would be a warning about nothing."""
    assert LevelMonitor().reading.quality is InputQuality.OK


def test_every_unusable_verdict_tells_the_user_what_to_do() -> None:
    for quality in InputQuality:
        if quality is InputQuality.OK:
            continue
        assert quality.advice, f"{quality} has no advice"
        assert "gain" in quality.advice or "muted" in quality.advice


# --------------------------------------------------------------------------------------
# The numbers
# --------------------------------------------------------------------------------------


def test_frame_levels_are_exact_for_a_known_frame() -> None:
    frame = array("h", [16384, -16384, 16384, -16384]).tobytes()

    peak, sum_squares, clipped, count = frame_levels(frame)

    assert count == 4
    assert clipped == 0
    assert peak == pytest.approx(16384 / FULL_SCALE)
    assert sum_squares == pytest.approx(4 * 16384.0**2)


def test_an_empty_frame_is_not_an_error() -> None:
    """A device hands over a zero-length buffer when a stream stops."""
    assert frame_levels(b"") == (0.0, 0.0, 0, 0)


def test_a_half_sample_frame_is_refused() -> None:
    with pytest.raises(ValueError, match="16-bit"):
        frame_levels(b"\x00\x01\x02")


def test_the_window_is_bounded() -> None:
    """The monitor must not grow with the length of the conversation."""
    monitor = LevelMonitor(window_frames=10)
    for _ in range(1000):
        monitor.observe(tone(0.5))

    assert len(monitor._frames) == 10


def test_a_window_of_less_than_one_frame_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one frame"):
        LevelMonitor(window_frames=0)


# --------------------------------------------------------------------------------------
# The decorator
# --------------------------------------------------------------------------------------


class FakeSource:
    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.closed = False

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat()

    def frames(self):  # type: ignore[no-untyped-def]
        yield from self._frames

    def close(self) -> None:
        self.closed = True


def test_frames_pass_through_unchanged() -> None:
    """A monitor that altered the audio would be a monitor that changed the transcript."""
    frames = [tone(0.5), square(), silence()]
    watched = LevelWatchingSource(FakeSource(list(frames)))

    assert list(watched.frames()) == frames


def test_the_verdict_follows_what_passed_through() -> None:
    watched = LevelWatchingSource(FakeSource([square()] * 60))

    for _ in watched.frames():
        pass

    assert watched.level.quality is InputQuality.CLIPPING


def test_the_decorator_exposes_both_verdicts() -> None:
    """`level` is what the window shows; `overall_level` is what a finished run reports."""
    watched = LevelWatchingSource(FakeSource([square()] * 60 + [silence()] * 60))

    for _ in watched.frames():
        pass

    assert watched.level.quality is InputQuality.SILENT
    assert watched.overall_level.quality is InputQuality.CLIPPING


def test_closing_closes_the_source_underneath() -> None:
    source = FakeSource([])
    watched = LevelWatchingSource(source)

    watched.close()

    assert source.closed is True


def test_it_retains_no_audio() -> None:
    """`docs/RETENTION_POLICY.md`: readings are OPERATIONAL_METADATA, frames are not."""
    watched = LevelWatchingSource(FakeSource([tone(0.5)] * 5))
    for _ in watched.frames():
        pass

    held = [value for slot in watched.__slots__ for value in [getattr(watched, slot)]]
    assert not any(isinstance(value, bytes) for value in held)
