"""The decision `measure_pauses.py` makes, checked without a microphone.

This script calibrates `SILENCE_AFTER_SPEECH_SECONDS` — how long a speaker must stop before
the recogniser calls an utterance finished. ADR 0024 records why no recording in this project
can answer that: published speech clips are trimmed and contain no natural pauses. So the
instrument runs live, which is also why it had no tests, and why its state machine now sits
in `PauseTracker` with `main` keeping the clock.

Its history is a reason to check it rather than an argument that it is fine. The script's own
docstring records an earlier version that seeded its noise floor from its first frame, went
deaf when armed mid-sentence, and detected 75.5% of frames against 5.2% on identical audio —
an instrument sharing a defect with the thing it was measuring.
"""

from __future__ import annotations

import numpy as np
import pytest
from measure_pauses import (
    ARM_FRAMES,
    FRAME_MS,
    SPEECH_RMS,
    TIMELINE_BUCKET_FRAMES,
    PauseTracker,
    percentile,
)

LOUD = SPEECH_RMS + 500.0
QUIET = SPEECH_RMS - 500.0


def tracker_over(frames: list[float]) -> PauseTracker:
    """Feed a whole sequence the way `main` does: arm first, then account."""
    tracker = PauseTracker()
    for rms in frames:
        if not tracker.armed:
            tracker.arm(rms)
            continue
        tracker.push(rms)
    return tracker


def frames(*runs: tuple[str, int]) -> list[float]:
    """`("loud", 5), ("quiet", 20)` to a list of frame rms values."""
    out: list[float] = []
    for kind, count in runs:
        out += [LOUD if kind == "loud" else QUIET] * count
    return out


# ---------------------------------------------------------------------------------------
# Arming
# ---------------------------------------------------------------------------------------


def test_a_single_loud_frame_does_not_arm_the_window() -> None:
    """A cough or a keystroke must not open the window and steal the measurement."""
    tracker = tracker_over(frames(("loud", ARM_FRAMES - 1), ("quiet", 50)))

    assert not tracker.armed


def test_sustained_speech_arms_the_window() -> None:
    tracker = tracker_over(frames(("loud", ARM_FRAMES), ("quiet", 10)))

    assert tracker.armed


def test_the_loud_frames_must_be_consecutive() -> None:
    """Four bursts of one loud frame are not a hundred milliseconds of speech."""
    scattered: list[float] = []
    for _ in range(ARM_FRAMES * 3):
        scattered += [LOUD, QUIET]

    assert not tracker_over(scattered).armed


def test_nothing_is_measured_before_the_window_opens() -> None:
    """Every earlier run of this measurement was corrupted by the gap between starting it
    and the speaker learning it had started; one measured 26 seconds of an empty room."""
    tracker = tracker_over(frames(("quiet", 500), ("loud", ARM_FRAMES)))

    assert tracker.speech_frames == 0
    assert tracker.gaps == []


# ---------------------------------------------------------------------------------------
# What counts as a gap
# ---------------------------------------------------------------------------------------


def test_a_silence_between_two_utterances_is_a_gap() -> None:
    tracker = tracker_over(frames(("loud", ARM_FRAMES + 5), ("quiet", 25), ("loud", 5)))

    assert tracker.gaps == [pytest.approx(25 * FRAME_MS / 1000)]


def test_several_gaps_are_recorded_in_the_order_they_happened() -> None:
    tracker = tracker_over(
        frames(
            ("loud", ARM_FRAMES + 5),
            ("quiet", 10),
            ("loud", 5),
            ("quiet", 30),
            ("loud", 5),
            ("quiet", 20),
            ("loud", 5),
        )
    )

    assert tracker.gaps == [pytest.approx(0.2), pytest.approx(0.6), pytest.approx(0.4)]


def test_the_silence_the_measurement_stops_in_is_not_a_gap() -> None:
    """It is the end of the window, not a pause between two utterances.

    Counting it would put a spuriously long value at the top of every percentile, and the
    p90 is what a threshold is chosen against.
    """
    tracker = tracker_over(frames(("loud", ARM_FRAMES + 5), ("quiet", 400)))

    assert tracker.gaps == []


def test_a_silence_before_the_first_word_of_the_armed_window_is_not_a_gap() -> None:
    """Arming happens on the fifth loud frame; a speaker who stops immediately after leaves
    a silence that began before any speech this window counted.

    It costs at most one gap out of dozens, and keeps every recorded value unambiguously a
    pause between two things that were said.
    """
    tracker = tracker_over(frames(("loud", ARM_FRAMES), ("quiet", 40), ("loud", 5)))

    assert tracker.armed
    assert tracker.gaps == []


def test_continuous_speech_produces_no_gaps() -> None:
    tracker = tracker_over(frames(("loud", 200)))

    assert tracker.gaps == []
    assert tracker.speech_frames == 200 - ARM_FRAMES


# ---------------------------------------------------------------------------------------
# The summary lines
# ---------------------------------------------------------------------------------------


def test_speech_seconds_counts_only_frames_above_the_threshold() -> None:
    tracker = tracker_over(frames(("loud", ARM_FRAMES), ("loud", 50), ("quiet", 50)))

    assert tracker.speech_seconds == pytest.approx(50 * FRAME_MS / 1000)


def test_the_loudest_frame_is_reported_so_the_threshold_can_be_judged() -> None:
    """Printed beside the threshold: a run where the loudest frame barely clears it was
    measuring a room, not a speaker."""
    tracker = tracker_over([LOUD] * ARM_FRAMES + [QUIET, 9000.0, QUIET])

    assert tracker.loudest == 9000.0


@pytest.mark.parametrize(
    ("talking_frames", "expected"),
    [(TIMELINE_BUCKET_FRAMES, "#"), (TIMELINE_BUCKET_FRAMES // 2 + 1, "#"), (1, "-"), (0, ".")],
)
def test_each_timeline_character_summarises_one_second(talking_frames: int, expected: str) -> None:
    """`#` talking, `-` partly, `.` quiet — a majority of the second decides `#`."""
    quiet = TIMELINE_BUCKET_FRAMES - talking_frames
    tracker = tracker_over(
        frames(("loud", ARM_FRAMES)) + frames(("loud", talking_frames), ("quiet", quiet))
    )

    assert tracker.timeline == [expected]


def test_a_part_second_at_the_end_is_not_reported_as_a_second() -> None:
    """The bucket only flushes when it is full, so the timeline never invents a character."""
    tracker = tracker_over(frames(("loud", ARM_FRAMES), ("loud", TIMELINE_BUCKET_FRAMES - 1)))

    assert tracker.timeline == []


# ---------------------------------------------------------------------------------------
# The percentile the thresholds are chosen against
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [(0.0, 1.0), (0.5, 3.0), (0.75, 4.0), (0.9, 5.0), (1.0, 5.0)],
)
def test_percentile_indexes_a_sorted_list_without_running_off_the_end(
    fraction: float, expected: float
) -> None:
    """`1.0` must return the largest value rather than raise, and it is asked for."""
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], fraction) == expected


def test_percentile_of_a_single_value_is_that_value() -> None:
    assert percentile([2.5], 0.9) == 2.5


# ---------------------------------------------------------------------------------------
# The extraction changed no behaviour
# ---------------------------------------------------------------------------------------


def inline_logic(sequence: list[float]) -> tuple[list[float], str, int, float, bool]:
    """`main`'s loop as it stood before `PauseTracker`, with the clock taken out.

    Kept here so the refactor is asserted rather than asserted-to-have-been-checked. If
    `PauseTracker` is changed deliberately, this has to change with it and the diff says so.
    """
    gaps: list[float] = []
    timeline: list[str] = []
    bucket: list[float] = []
    silence_run = speech_frames = consecutive = 0
    loudest = 0.0
    heard_speech = armed = False
    for rms in sequence:
        if not armed:
            consecutive = consecutive + 1 if rms > SPEECH_RMS else 0
            if consecutive >= ARM_FRAMES:
                armed = True
            continue
        loudest = max(loudest, rms)
        bucket.append(rms)
        if len(bucket) >= TIMELINE_BUCKET_FRAMES:
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
    return gaps, "".join(timeline), speech_frames, loudest, armed


def test_the_tracker_agrees_with_the_loop_it_replaced() -> None:
    """Three hundred random frame sequences, seeded so a failure is reproducible."""
    rng = np.random.default_rng(1)
    for _ in range(300):
        count = int(rng.integers(0, 900))
        loud = rng.random(count) < 0.5
        sequence = [
            float(rng.uniform(SPEECH_RMS, 6000.0) if is_loud else rng.uniform(0.0, SPEECH_RMS))
            for is_loud in loud
        ]
        tracker = tracker_over(sequence)
        assert (
            tracker.gaps,
            "".join(tracker.timeline),
            tracker.speech_frames,
            tracker.loudest,
            tracker.armed,
        ) == inline_logic(sequence)
