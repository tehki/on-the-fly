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
from on_the_fly.domain.audio.levels import FULL_SCALE
from on_the_fly.domain.audio.settling import (
    DEFAULT_MAX_SETTLE_MS,
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
