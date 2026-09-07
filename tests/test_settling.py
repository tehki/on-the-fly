"""Tests for capture settling (ADR 0020).

The transient these guard against is real and measured: a cold analog input sits pinned at
the negative rail for around half a second and takes about 1.8 s to centre, and everything
downstream treats it as audio. What is asserted here is the shape of the response — drop
while it looks like a rail, release as soon as it does not, never hold audio, and never
withhold a microphone indefinitely.

Frames are synthesised. The thresholds were calibrated against the real capture path; the
measurements are in `docs/adr/0020-capture-settling.md`.
"""

from __future__ import annotations

from array import array

import pytest

from on_the_fly.domain.audio.formats import AudioFormat
from on_the_fly.domain.audio.levels import CLIPPED_SAMPLE, FULL_SCALE
from on_the_fly.domain.audio.settling import (
    DEFAULT_MAX_SETTLE_MS,
    DEFAULT_WINDOW_MS,
    SettlingSource,
)

SAMPLES_PER_FRAME = 320  # 20 ms at 16 kHz
FRAME_MS = 20


def rail(samples: int = SAMPLES_PER_FRAME) -> bytes:
    """The converter pinned at the negative rail: DC -1.0, every sample clipped."""
    return array("h", [-32768] * samples).tobytes()


def offset(fraction: float, samples: int = SAMPLES_PER_FRAME) -> bytes:
    """A DC offset with no signal on it — the middle of the transient."""
    value = int(fraction * FULL_SCALE)
    return array("h", [value] * samples).tobytes()


def speech(samples: int = SAMPLES_PER_FRAME, *, amplitude: float = 0.05) -> bytes:
    """Centred audio at a speech-like level: alternating, so DC is zero."""
    value = int(amplitude * FULL_SCALE)
    return array("h", [value if i % 2 else -value for i in range(samples)]).tobytes()


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


def frames_for_ms(frame: bytes, milliseconds: int) -> list[bytes]:
    return [frame] * (milliseconds // FRAME_MS)


def test_a_warm_device_loses_nothing_measurable() -> None:
    """The transient is cold-start only, so a clean input must not be charged for it."""
    source = FakeSource(frames_for_ms(speech(), 1000))
    settling = SettlingSource(source)

    passed = list(settling.frames())

    # One window is examined before anything can be judged; everything after it is audio.
    assert settling.is_settled
    assert not settling.gave_up
    assert settling.discarded_ms == 240
    assert len(passed) == 50 - 240 // FRAME_MS


def test_the_rail_is_dropped_and_the_audio_after_it_is_not() -> None:
    source = FakeSource(frames_for_ms(rail(), 600) + frames_for_ms(speech(), 400))
    settling = SettlingSource(source)

    passed = list(settling.frames())

    assert settling.is_settled
    assert not settling.gave_up
    # 600 ms of rail, then a window of clean audio before it can be called settled.
    assert settling.discarded_ms == 840
    assert passed == frames_for_ms(speech(), 160)


def test_a_decaying_offset_is_dropped_until_it_is_centred() -> None:
    """The real transient is not a rail throughout — it decays through DC offset."""
    decaying = [offset(level) for level in (0.9, 0.7, 0.5, 0.3, 0.2, 0.1, 0.06)]
    # Each level held for a full window, so none of them can be judged settled early.
    ramp = [frame for level in decaying for frame in [level] * (240 // FRAME_MS)]
    source = FakeSource(ramp + frames_for_ms(speech(), 500))
    settling = SettlingSource(source)

    passed = list(settling.frames())

    assert settling.is_settled
    assert not settling.gave_up
    # Nothing above the DC threshold gets through, including the 0.06 tail.
    assert all(frame == speech() for frame in passed)


def test_an_input_that_never_settles_is_released_rather_than_swallowed() -> None:
    """A microphone that produces nothing usable must still produce something."""
    source = FakeSource(frames_for_ms(rail(), 5000))
    settling = SettlingSource(source)

    passed = list(settling.frames())

    assert settling.is_settled
    assert settling.gave_up
    assert settling.discarded_ms == DEFAULT_MAX_SETTLE_MS
    assert len(passed) == (5000 - DEFAULT_MAX_SETTLE_MS) // FRAME_MS


def test_clipping_alone_holds_the_input_back() -> None:
    """Full-scale audio with no DC offset is still the rail, alternating."""
    saturated = array("h", [32767 if i % 2 else -32768 for i in range(SAMPLES_PER_FRAME)]).tobytes()
    source = FakeSource(frames_for_ms(saturated, 500) + frames_for_ms(speech(), 500))
    settling = SettlingSource(source)

    passed = list(settling.frames())

    assert settling.discarded_ms == 740
    assert passed == frames_for_ms(speech(), 260)


def test_frames_pass_through_unchanged_once_settled() -> None:
    """The bytes the pipeline reads are the bytes the device produced."""
    wanted = [speech(amplitude=level) for level in (0.05, 0.1, 0.2, 0.05)]
    source = FakeSource(frames_for_ms(speech(), 240) + wanted)
    settling = SettlingSource(source)

    assert list(settling.frames()) == wanted


def test_it_holds_no_audio() -> None:
    """Counters, never frames — the property `docs/RETENTION_POLICY.md` depends on."""
    source = FakeSource(frames_for_ms(rail(), 400) + frames_for_ms(speech(), 400))
    settling = SettlingSource(source)
    list(settling.frames())

    held = b"".join(
        value
        for value in (getattr(settling, slot, None) for slot in SettlingSource.__slots__)
        if isinstance(value, bytes)
    )
    assert held == b""
    for entry in settling._window:
        assert all(isinstance(number, (int, float)) for number in entry)


def test_closing_closes_the_wrapped_source() -> None:
    source = FakeSource([])
    settling = SettlingSource(source)

    settling.close()

    assert source.closed


def test_the_format_is_the_wrapped_source_s() -> None:
    settling = SettlingSource(FakeSource([]))

    assert settling.audio_format == AudioFormat()


@pytest.mark.parametrize(
    ("window_ms", "max_settle_ms"),
    [(0, 3000), (-20, 3000), (250, 200)],
)
def test_a_configuration_that_cannot_work_is_refused(window_ms: int, max_settle_ms: int) -> None:
    with pytest.raises(ValueError):
        SettlingSource(FakeSource([]), window_ms=window_ms, max_settle_ms=max_settle_ms)


def test_a_frame_that_is_not_whole_samples_is_refused() -> None:
    """Device buffers are untrusted input (Article 4), the same as everywhere else."""
    settling = SettlingSource(FakeSource([b"\x00\x01\x02"]))

    with pytest.raises(ValueError, match="whole number of 16-bit samples"):
        list(settling.frames())


# --------------------------------------------------------------------------------------
# The boundaries of "settled"
#
# The tests above assert the shape of the response — drop while it looks like a rail,
# release as soon as it does not. Mutation testing found that both comparisons deciding
# *when* that happens could be flipped without any of them noticing, along with the guard
# that refuses to judge on a partial window and the one that keeps a frame in it.
#
# The thresholds were calibrated against the real capture path in ADR 0020. What is pinned
# here is that the release turns on the numbers that ADR chose.
# --------------------------------------------------------------------------------------

WINDOW_SAMPLES = 16_000 * DEFAULT_WINDOW_MS // 1000  # 3840, twelve 20 ms frames


def dc_counts(counts: int, samples: int = SAMPLES_PER_FRAME) -> bytes:
    """A flat DC offset of exactly `counts`, so the window's DC is exactly that."""
    return array("h", [counts] * samples).tobytes()


def clipped_frame(clipped: int, samples: int = SAMPLES_PER_FRAME) -> bytes:
    """`clipped` samples at the rail, the rest alternating so the window's DC stays zero.

    The rail samples are paired, one positive and one negative, so raising the clipped
    count does not also move the DC and decide the verdict by the other threshold.
    """
    values = []
    for index in range(samples):
        if index < clipped:
            values.append(CLIPPED_SAMPLE if index % 2 else -CLIPPED_SAMPLE)
        else:
            values.append(1 if index % 2 else -1)
    return array("h", values).tobytes()


def released(frames: list[bytes]) -> list[bytes]:
    """What a `SettlingSource` passes through for a given input."""
    return list(SettlingSource(FakeSource(frames)).frames())


def test_a_dc_offset_one_count_under_the_threshold_settles() -> None:
    """1638 counts is 0.049989 of full scale; the threshold is 0.05.

    A full window of it, then one frame that must be passed through — if the window had not
    settled, nothing would come out until the cap.
    """
    window = frames_for_ms(dc_counts(1638), DEFAULT_WINDOW_MS)

    assert released([*window, speech()]), "a centred-enough window was not released"


def test_a_dc_offset_one_count_over_the_threshold_does_not_settle() -> None:
    """1639 counts is 0.050020. The transient passes 0.13 as late as 1300 ms, so the line
    has to be the line."""
    window = frames_for_ms(dc_counts(1639), DEFAULT_WINDOW_MS)

    assert released([*window, dc_counts(1639)]) == [], "an off-centre window was released"


def test_clipping_one_sample_under_the_threshold_settles() -> None:
    """38 clipped samples of a 3840-sample window is 0.009896; the threshold is 0.01."""
    window = [clipped_frame(0)] * 11 + [clipped_frame(38)]

    assert released([*window, speech()]), "a barely-clipped window was not released"


def test_clipping_one_sample_over_the_threshold_does_not_settle() -> None:
    """39 is 0.010156. Clipping during settling is the rail, not a loud noise, and a
    settled window on the reference machine measures at most 1%."""
    window = [clipped_frame(0)] * 11 + [clipped_frame(39)]

    assert released([*window, clipped_frame(39)]) == []


def test_the_verdict_waits_for_a_whole_window() -> None:
    """Deciding from a fraction of a window is how one quiet frame inside the transient
    would release it early — which is the failure the window length exists for."""
    settled_frame = speech()
    one_short = [settled_frame] * (WINDOW_SAMPLES // SAMPLES_PER_FRAME - 1)

    assert released(one_short) == [], "released before a full window had been seen"
    assert released([*one_short, settled_frame, settled_frame])


def test_a_settle_cap_equal_to_one_window_is_allowed() -> None:
    """The guard refuses a cap below one window, because the input would be released
    before it had been looked at. Exactly one window is the shortest that can be."""
    source = SettlingSource(
        FakeSource([]), window_ms=DEFAULT_WINDOW_MS, max_settle_ms=DEFAULT_WINDOW_MS
    )

    assert source.discarded_ms == 0


def test_a_window_of_one_millisecond_is_allowed() -> None:
    """The guard is on zero and below. One is meaningless in practice and legal, and the
    refusal must not creep upward into values somebody chose."""
    assert SettlingSource(FakeSource([]), window_ms=1, max_settle_ms=1000) is not None


def test_nothing_is_dropped_once_the_input_has_been_released() -> None:
    """The rail can come back — a loud noise clips too — and re-settling mid-session would
    swallow speech. Settling is a start-up decision, made once."""
    window = frames_for_ms(speech(), DEFAULT_WINDOW_MS)
    later_rail = rail()

    passed = released([*window, speech(), later_rail, speech()])

    assert later_rail in passed, "a clipped frame after settling was dropped"


# The clipped fraction is the one threshold a real window can sit exactly on: at 16 kHz a
# 250 ms window is 4000 samples and one percent of that is 40. (The DC threshold is not —
# 0.05 of full scale over 4000 samples works out to 0.049999999999999996, so `<` and `<=`
# agree there whatever frames are fed in.)

QUARTER_SECOND_MS = 250
LONG_FRAME_SAMPLES = 400  # ten of these fill the window exactly


class WideFrameSource(FakeSource):
    """A source whose frames are 400 samples rather than 320."""

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat()


def clipped_pairs(clipped: int) -> bytes:
    """A 400-sample frame with `clipped` samples at the rail, paired so DC stays zero."""
    values = []
    for index in range(LONG_FRAME_SAMPLES):
        if index < clipped:
            values.append(CLIPPED_SAMPLE if index % 2 else -CLIPPED_SAMPLE)
        else:
            values.append(1 if index % 2 else -1)
    return array("h", values).tobytes()


def quarter_second_release(per_frame_clipped: int) -> list[bytes]:
    window = [clipped_pairs(per_frame_clipped)] * 10
    source = WideFrameSource([*window, clipped_pairs(per_frame_clipped)])
    return list(SettlingSource(source, window_ms=QUARTER_SECOND_MS, max_settle_ms=1000).frames())


def test_a_window_exactly_one_percent_clipped_does_not_settle() -> None:
    """`clipped < SETTLED_CLIPPED_FRACTION`. Forty samples of four thousand is the line,
    and at the line the input has not come up yet."""
    assert quarter_second_release(4) == [], "a window at exactly 1% clipped was released"


def test_a_window_under_one_percent_clipped_settles() -> None:
    """Thirty of four thousand is 0.75%, and a settled window on the reference machine
    measures at most one percent."""
    assert quarter_second_release(3), "a window under the threshold was held back"


def test_the_window_holds_only_as_much_audio_as_it_was_asked_for() -> None:
    """A window that keeps an extra frame keeps the transient in view after it has passed.

    With a window of one frame, the first clean frame is the whole window and settles it —
    that frame is observed and dropped, as every settling frame is, and the next one is
    yielded. A window holding two would still be judging the rail alongside it, so nothing
    would come out at all.
    """
    clean = speech()
    passed = list(
        SettlingSource(
            FakeSource([rail(), rail(), clean, clean]), window_ms=FRAME_MS, max_settle_ms=1000
        ).frames()
    )

    assert passed == [clean], "the window outlived the transient it was measuring"
