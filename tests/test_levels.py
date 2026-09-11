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

import dataclasses
import math
from array import array

import pytest

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.levels import (
    CLIPPED_SAMPLE,
    CLIPPING_FRACTION,
    DEFAULT_WINDOW_FRAMES,
    FLOOR_WINDOW_FRAMES,
    FULL_SCALE,
    LOUD_FLOOR,
    QUIET_RMS,
    SILENT_PEAK,
    InputQuality,
    LevelMonitor,
    LevelReading,
    LevelWatchingSource,
    _classify,
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


# ======================================================================================
# The floor, and the input that never goes quiet (ADR 0021)
# ======================================================================================


def steady(amplitude: float, frames: int) -> list[bytes]:
    """A signal that never pauses — a room with the gain wound up."""
    return [tone(amplitude) for _ in range(frames)]


def speech_like(amplitude: float, frames: int, *, pause_every: int = 5) -> list[bytes]:
    """Loud stretches separated by quiet ones, which is what distinguishes talking."""
    return [
        tone(amplitude) if index % pause_every else tone(amplitude / 200) for index in range(frames)
    ]


def observe_all(monitor: LevelMonitor, frames: list[bytes]) -> LevelReading:
    for frame in frames:
        monitor.observe(frame)
    return monitor.reading


def test_an_input_that_never_goes_quiet_is_too_loud() -> None:
    """The failure that started this: an amplified room, transcribed as words nobody said."""
    monitor = LevelMonitor()

    reading = observe_all(monitor, steady(0.5, FLOOR_WINDOW_FRAMES))

    assert reading.quality is InputQuality.TOO_LOUD
    assert reading.floor is not None
    assert reading.floor >= LOUD_FLOOR


def test_loud_speech_is_not_condemned_for_being_loud() -> None:
    """Amplified speech transcribes correctly; a warning here would be a false alarm.

    Measured: the same recording at 24x gain, 21% of its samples at full scale, still
    transcribed word for word (ADR 0021). Only its pauses keep it out of `TOO_LOUD`.
    """
    monitor = LevelMonitor()

    reading = observe_all(monitor, speech_like(0.9, FLOOR_WINDOW_FRAMES))

    assert reading.quality is not InputQuality.TOO_LOUD


def test_a_quiet_room_is_not_too_loud_however_flat_it_is() -> None:
    """A room with no pauses is only a problem when it is also loud enough to recognise."""
    monitor = LevelMonitor()

    reading = observe_all(monitor, steady(0.02, FLOOR_WINDOW_FRAMES))

    assert reading.quality is not InputQuality.TOO_LOUD


def test_the_verdict_waits_for_a_full_window() -> None:
    """Five seconds, because at one second loud speech and a loud room are the same."""
    monitor = LevelMonitor()

    reading = observe_all(monitor, steady(0.5, FLOOR_WINDOW_FRAMES - 1))

    assert reading.floor is None
    assert reading.quality is not InputQuality.TOO_LOUD


def test_clipping_is_reported_ahead_of_too_loud() -> None:
    """The more specific statement about the same problem, and it needs less audio."""
    monitor = LevelMonitor()

    reading = observe_all(monitor, [square() for _ in range(FLOOR_WINDOW_FRAMES)])

    assert reading.quality is InputQuality.CLIPPING


def test_a_finished_recording_is_judged_on_its_worst_stretch() -> None:
    """A loud room followed by a silent tail must not be excused by the tail."""
    monitor = LevelMonitor()
    for frame in steady(0.5, FLOOR_WINDOW_FRAMES):
        monitor.observe(frame)
    for frame in steady(0.0001, FLOOR_WINDOW_FRAMES):
        monitor.observe(frame)

    assert monitor.reading.quality is not InputQuality.TOO_LOUD
    assert monitor.overall.quality is InputQuality.TOO_LOUD


def test_the_floor_is_a_number_and_the_reading_still_holds_no_audio() -> None:
    monitor = LevelMonitor()
    reading = observe_all(monitor, steady(0.5, FLOOR_WINDOW_FRAMES))

    assert isinstance(reading.floor, float)
    assert "floor" in str(reading)


def test_a_recording_too_short_to_have_a_floor_reports_none_rather_than_zero() -> None:
    """`overall` used to return the initial 0.0 for any recording shorter than the floor
    window, so a 3.8-second clip printed `floor 0.000` — a number nobody had measured, in
    the line that tells a user whether their microphone is usable.

    It never misclassified, because 0.0 is below every threshold. It stated a measurement
    that had not been taken, which is the fault this project treats as the worse one
    (ADR 0026 declines to give a verdict at all rather than give one it cannot support).
    """
    monitor = LevelMonitor()
    observe_all(monitor, steady(0.5, FLOOR_WINDOW_FRAMES - 1))

    assert monitor.overall.floor is None
    assert "floor" not in str(monitor.overall)


def test_one_more_frame_is_enough_to_have_a_floor() -> None:
    """The boundary the previous test sits just below, so neither can pass by accident."""
    monitor = LevelMonitor()
    observe_all(monitor, steady(0.5, FLOOR_WINDOW_FRAMES))

    assert monitor.overall.floor is not None
    assert "floor" in str(monitor.overall)


def test_resetting_forgets_the_floor_from_the_overall_reading_too() -> None:
    """`reset` restores "not measured", not "measured as zero"."""
    monitor = LevelMonitor()
    observe_all(monitor, steady(0.5, FLOOR_WINDOW_FRAMES))
    assert monitor.overall.floor is not None

    monitor.reset()

    assert monitor.overall.floor is None


def test_resetting_forgets_the_floor() -> None:
    monitor = LevelMonitor()
    observe_all(monitor, steady(0.5, FLOOR_WINDOW_FRAMES))

    monitor.reset()

    assert monitor.reading.floor is None
    assert monitor.overall.quality is InputQuality.OK


def test_every_verdict_but_ok_tells_the_user_what_to_do() -> None:
    for quality in InputQuality:
        if quality is InputQuality.OK:
            assert quality.advice == ""
        else:
            assert quality.advice, f"{quality} gives the user nothing to act on"
            assert not quality.is_usable


def test_a_floor_window_that_cannot_work_is_refused() -> None:
    with pytest.raises(ValueError):
        LevelMonitor(floor_window_frames=0)


# --------------------------------------------------------------------------------------
# The boundaries themselves
#
# This file opens by saying its tests are "mostly about classification being right at the
# boundaries". They were about the verdicts — clipped audio is reported, silence is
# reported, clipping outranks the rest — and mutation testing found that all four
# comparisons in `_classify` could be flipped without any of them noticing.
#
# The thresholds were calibrated against real speech in ADR 0019. What is pinned here is
# that the classifier turns on the numbers that ADR chose, and not one step either side.
# --------------------------------------------------------------------------------------


def flat(amplitude: int, samples: int = SAMPLES_PER_FRAME) -> bytes:
    """A frame alternating +/- one amplitude, so peak and RMS are both exactly that.

    A sine would put peak and RMS a factor of root two apart, which is right for audio and
    useless for landing on one threshold at a time.
    """
    return array("h", (amplitude if i % 2 == 0 else -amplitude for i in range(samples))).tobytes()


def partly_clipped(clipped: int, samples: int = SAMPLES_PER_FRAME) -> bytes:
    """`clipped` samples at the rails; the rest at a comfortable level.

    The remainder is loud enough that neither SILENT nor QUIET can be what decides the
    verdict, so the only threshold in play is the clipped fraction.
    """
    values = [CLIPPED_SAMPLE if i < clipped else (8000 if i % 2 else -8000) for i in range(samples)]
    return array("h", values).tobytes()


def verdict_of(frame: bytes) -> InputQuality:
    """The verdict for a single frame, through the public surface."""
    monitor = LevelMonitor(window_frames=1)
    return monitor.observe(frame).quality


def test_a_frame_exactly_at_the_clipping_fraction_is_clipping() -> None:
    """Sixteen samples of 320 is exactly five percent. The comment on the constant explains
    why it sits where it does: one 20 ms frame fully clipped is 2% of a one-second window,
    so a door slam must not reach it."""
    assert verdict_of(partly_clipped(16)) is InputQuality.CLIPPING
    assert verdict_of(partly_clipped(15)) is not InputQuality.CLIPPING


def test_a_peak_exactly_at_the_silence_threshold_is_not_silent() -> None:
    """`peak < SILENT_PEAK`. At the threshold there is sound, and calling it silence would
    tell someone their microphone is muted when it is working."""
    assert verdict_of(flat(65)) is InputQuality.SILENT
    assert verdict_of(flat(66)) is not InputQuality.SILENT


def test_an_rms_exactly_at_the_quiet_threshold_is_not_quiet() -> None:
    """`rms < QUIET_RMS`, and the same argument: at the threshold the input is usable."""
    assert verdict_of(flat(163)) is InputQuality.QUIET
    assert verdict_of(flat(164)) is InputQuality.OK


def test_a_floor_exactly_at_the_loud_threshold_is_too_loud() -> None:
    """`floor >= LOUD_FLOOR`, unlike the two above. A floor that never drops means the room
    itself is being transcribed, and the threshold is the point at which that starts."""
    for amplitude, expected in ((4916, InputQuality.TOO_LOUD), (4915, InputQuality.OK)):
        monitor = LevelMonitor(window_frames=1, floor_window_frames=FLOOR_WINDOW_FRAMES)
        for _ in range(FLOOR_WINDOW_FRAMES):
            reading = monitor.observe(flat(amplitude))

        assert reading.quality is expected, f"a floor of {amplitude / FULL_SCALE:.6f}"


# --- the order the verdicts are asked in --------------------------------------------------


def test_silence_is_reported_ahead_of_quiet() -> None:
    """A silent frame is also a quiet one. Saying "very quiet, turn the gain up" to someone
    whose microphone is muted sends them to the wrong control."""
    quiet_too = flat(65)
    peak, squares, _, samples = frame_levels(quiet_too)

    assert peak < 0.002 and math.sqrt(squares / samples) / FULL_SCALE < 0.005
    assert verdict_of(quiet_too) is InputQuality.SILENT


def test_a_floor_that_never_drops_outranks_a_quiet_window() -> None:
    """The floor is measured over a longer window than the rest of the verdict.

    So an input can be loud for several seconds and quiet in the last one, and the advice
    that matters is still about the gain being far too high — the quiet moment is what the
    speaker did, not what the device is doing.
    """
    monitor = LevelMonitor(window_frames=1, floor_window_frames=FLOOR_WINDOW_FRAMES)
    for _ in range(FLOOR_WINDOW_FRAMES):
        monitor.observe(flat(6000))

    reading = monitor.observe(silence())

    assert reading.peak == 0.0, "the current window really is silent"
    assert reading.quality is InputQuality.TOO_LOUD


# --- what counts as a clipped sample -------------------------------------------------------


def test_the_clipping_mark_sits_below_full_scale_on_purpose() -> None:
    """Resampling and dithering shave a count or two off a saturated signal without making
    it less saturated, so a sample need not reach 32767 to be clipped."""
    assert CLIPPED_SAMPLE < FULL_SCALE


def test_a_sample_exactly_at_the_clipping_mark_counts_as_clipped() -> None:
    """And one below does not — the mark is where the counting starts."""
    at_the_mark = array("h", [CLIPPED_SAMPLE] * SAMPLES_PER_FRAME).tobytes()
    just_under = array("h", [CLIPPED_SAMPLE - 1] * SAMPLES_PER_FRAME).tobytes()

    assert frame_levels(at_the_mark)[2] == SAMPLES_PER_FRAME
    assert frame_levels(just_under)[2] == 0


def test_a_negative_sample_at_the_mark_counts_too() -> None:
    """Clipping is symmetric; a signal pinned to the negative rail is just as distorted."""
    negative = array("h", [-CLIPPED_SAMPLE] * SAMPLES_PER_FRAME).tobytes()

    assert frame_levels(negative)[2] == SAMPLES_PER_FRAME


# --- the direction of each comparison ------------------------------------------------------
#
# Three of the four thresholds cannot be reached exactly by a real frame: 0.002, 0.005 and
# 0.15 of full scale are 65.534, 163.835 and 4915.05 in sixteen-bit counts, and samples are
# integers. So whether those comparisons are `<` or `<=` makes no difference to any audio —
# but the tests above state a direction in words, and a claim in a docstring that nothing
# checks is the thing this repository keeps finding. Asked of the classifier directly.


def test_a_reading_exactly_on_each_threshold_falls_the_documented_way() -> None:
    """At the threshold: still sound, still usable, and already too loud.

    The two quiet limits are exclusive — at the line the input is working, and telling
    someone their microphone is muted when it is not sends them to the wrong control. The
    loud limit is inclusive, because a floor that has reached the line is already the room
    being transcribed.
    """
    assert _classify(peak=SILENT_PEAK, rms=1.0, clipped_fraction=0.0) is InputQuality.OK
    assert _classify(peak=1.0, rms=QUIET_RMS, clipped_fraction=0.0) is InputQuality.OK
    assert (
        _classify(peak=1.0, rms=1.0, clipped_fraction=0.0, floor=LOUD_FLOOR)
        is InputQuality.TOO_LOUD
    )
    assert _classify(peak=1.0, rms=1.0, clipped_fraction=CLIPPING_FRACTION) is InputQuality.CLIPPING


def test_a_reading_one_step_inside_each_threshold_falls_the_other_way() -> None:
    """The same four, from the side that trips them."""
    assert _classify(peak=SILENT_PEAK / 2, rms=1.0, clipped_fraction=0.0) is InputQuality.SILENT
    assert _classify(peak=1.0, rms=QUIET_RMS / 2, clipped_fraction=0.0) is InputQuality.QUIET
    assert (
        _classify(peak=1.0, rms=1.0, clipped_fraction=0.0, floor=LOUD_FLOOR * 0.99)
        is InputQuality.OK
    )
    assert (
        _classify(peak=1.0, rms=1.0, clipped_fraction=CLIPPING_FRACTION * 0.99) is InputQuality.OK
    )


def test_no_floor_yet_is_not_a_loud_floor() -> None:
    """`None` means not enough audio to judge on, and must not be read as a verdict."""
    assert _classify(peak=1.0, rms=1.0, clipped_fraction=0.0, floor=None) is InputQuality.OK


# --- readings before there is anything to read ---------------------------------------------


def test_an_empty_monitor_reports_zeros_rather_than_a_measurement() -> None:
    """`OK` with a peak in it would be a number nobody measured, shown beside the ones that
    were. Both readings have to be empty, not merely uncomplaining."""
    monitor = LevelMonitor(window_frames=4)

    for reading in (monitor.reading, monitor.overall):
        assert (reading.peak, reading.rms, reading.clipped_fraction) == (0.0, 0.0, 0.0)
        assert reading.quality is InputQuality.OK
        assert reading.floor is None


def test_a_floor_window_of_one_frame_is_allowed() -> None:
    """One is the smallest window that can exist, and the guard refuses below it."""
    assert LevelMonitor(window_frames=1, floor_window_frames=1) is not None

    with pytest.raises(ValueError, match="floor window"):
        LevelMonitor(window_frames=1, floor_window_frames=0)


def test_the_floor_arrives_on_the_frame_that_completes_its_window() -> None:
    """Not one frame later. The window is how much evidence ADR 0021 asks for, and waiting
    past it would mean the verdict never settles on a short recording."""
    monitor = LevelMonitor(window_frames=1, floor_window_frames=3)

    assert monitor.observe(flat(6000)).floor is None
    assert monitor.observe(flat(6000)).floor is None
    assert monitor.observe(flat(6000)).floor is not None


def test_a_finished_recording_reports_the_numbers_it_actually_saw() -> None:
    """The running totals, pinned rather than left to arithmetic nobody checks.

    `overall` is what a finished file is judged on, and it is accumulated separately from
    the rolling window. Its counters start somewhere, and a start that is not zero is a
    measurement of audio that never arrived — small enough to look plausible and shown
    beside numbers that are real.
    """
    monitor = LevelMonitor(window_frames=2)
    for _ in range(4):
        monitor.observe(flat(3000))

    overall = monitor.overall
    assert overall.peak == pytest.approx(3000 / FULL_SCALE)
    assert overall.rms == pytest.approx(3000 / FULL_SCALE)
    assert overall.clipped_fraction == 0.0

    monitor.reset()
    for _ in range(4):
        monitor.observe(flat(1500))

    after = monitor.overall
    assert after.peak == pytest.approx(1500 / FULL_SCALE), "reset kept the louder past"
    assert after.rms == pytest.approx(1500 / FULL_SCALE)
    assert after.clipped_fraction == 0.0


# ---------------------------------------------------------------------------------------
# The three numbers ADR 0019 and ADR 0021 chose
#
# Every test above uses these constants to build its input, so all of them move together
# when one is edited and none of them notices. What follows pins the values themselves —
# not because a number is sacred, but because each was chosen against a measurement and
# changing it should mean revisiting that measurement.
# ---------------------------------------------------------------------------------------


def test_the_clipping_threshold_sits_just_below_full_scale() -> None:
    """A sample at 32700 of 32767 is 0.2 dB from the top: the analog path is already
    saturated there, and waiting for the exact maximum would count a saturated signal as
    clean (ADR 0019)."""
    assert CLIPPED_SAMPLE == 32_700
    assert CLIPPED_SAMPLE < FULL_SCALE


def test_a_sample_on_the_threshold_counts_as_clipped() -> None:
    """The boundary itself, which every other test in this file steps over rather than onto."""
    _, _, clipped_at, count = frame_levels(array("h", [CLIPPED_SAMPLE] * 160).tobytes())
    _, _, clipped_below, _ = frame_levels(array("h", [CLIPPED_SAMPLE - 1] * 160).tobytes())

    assert (clipped_at, count) == (160, 160)
    assert clipped_below == 0


def test_the_floor_window_is_the_five_seconds_that_separates_speech_from_a_room() -> None:
    """ADR 0021 measured it: at one second, heavily amplified speech and an amplified room
    are indistinguishable (0.45 against 0.43); at five they are not (0.12 against 0.33)."""
    assert FLOOR_WINDOW_FRAMES * 20 == 5_000, "the floor window is no longer five seconds"


def test_the_level_window_is_one_second() -> None:
    """Long enough that one loud syllable does not condemn a device, short enough that a user
    who fixes their gain sees the warning clear while they are still looking at it."""
    assert DEFAULT_WINDOW_FRAMES * 20 == 1_000


def test_a_reading_cannot_be_rewritten() -> None:
    """It is the evidence behind an accusation about somebody's microphone."""
    reading = LevelReading(peak=0.5, rms=0.2, clipped_fraction=0.0, quality=InputQuality.OK)

    with pytest.raises(dataclasses.FrozenInstanceError):
        reading.peak = 1.0  # type: ignore[misc]
