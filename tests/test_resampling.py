"""Tests for capture resampling (ADR 0013).

These use the real `av` resampler rather than a fake, because the thing worth testing is
that audio survives the conversion. A fake that returned the right number of bytes would
pass every structural assertion and tell us nothing about whether a 440 Hz tone is still a
440 Hz tone afterwards — and silent degradation is exactly the failure this module exists
to avoid.
"""

from __future__ import annotations

import math
import struct

import pytest

from on_the_fly.infrastructure.audio.backend import AudioDeviceError
from on_the_fly.infrastructure.audio.resampling import CANDIDATE_RATES, Resampler

FRAME_BYTES = 640  # 20 ms of 16 kHz mono int16


def tone(frequency_hz: float, seconds: float, rate: int, amplitude: int = 12000) -> bytes:
    count = int(rate * seconds)
    samples = [
        int(amplitude * math.sin(2 * math.pi * frequency_hz * i / rate)) for i in range(count)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


def dominant_frequency(pcm: bytes, rate: int) -> float:
    """Crude spectral peak, good enough to tell 440 Hz from an aliased artefact."""
    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if len(samples) < 64:
        return 0.0
    windowed = samples * np.hanning(len(samples))
    spectrum = np.abs(np.fft.rfft(windowed))
    return float(np.fft.rfftfreq(len(samples), 1.0 / rate)[int(np.argmax(spectrum))])


# --------------------------------------------------------------------------------------
# The point of the module: audio survives
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("source_rate", [48000, 44100, 32000, 22050])
def test_a_tone_keeps_its_pitch_across_the_conversion(source_rate: int) -> None:
    """440 Hz in, 440 Hz out. A resampler that got this wrong would still return bytes."""
    resampler = Resampler(source_rate_hz=source_rate, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    frames = resampler.push(tone(440.0, 0.5, source_rate))
    converted = b"".join(frames)

    assert converted, "half a second of audio must produce whole frames"
    assert abs(dominant_frequency(converted, 16000) - 440.0) < 25.0


def test_content_above_the_new_nyquist_does_not_alias_down_into_speech() -> None:
    """The reason this is not hand-written arithmetic.

    A 7 kHz tone cannot exist at 16 kHz — its Nyquist limit is 8 kHz, so it is near the
    edge — but a 15 kHz tone cannot, and naive decimation would fold it down into the
    speech band as a spurious low tone. A filtered resampler attenuates it instead.
    """
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    converted = b"".join(resampler.push(tone(15000.0, 0.5, 48000)))

    import numpy as np

    samples = np.frombuffer(converted, dtype=np.int16).astype(np.float64)
    # Aliasing would reproduce it at full amplitude somewhere in band; filtering leaves
    # little behind. The threshold is loose because the point is orders of magnitude.
    assert float(np.abs(samples).max()) < 6000.0


# --------------------------------------------------------------------------------------
# Frame size: the domain's contract does not change
# --------------------------------------------------------------------------------------


def test_every_returned_frame_is_exactly_one_frame() -> None:
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    frames = resampler.push(tone(440.0, 0.25, 48000))

    assert frames
    assert all(len(frame) == FRAME_BYTES for frame in frames)


def test_a_block_too_short_to_complete_a_frame_returns_nothing_yet() -> None:
    """Not an error. The audio is held until enough arrives."""
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    frames = resampler.push(tone(440.0, 0.002, 48000))

    assert frames == []
    assert resampler.pending_bytes >= 0


def test_audio_held_back_is_emitted_once_the_next_block_completes_it() -> None:
    """Nothing is dropped at a block boundary; a word split across reads survives."""
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    first = resampler.push(tone(440.0, 0.01, 48000))
    second = resampler.push(tone(440.0, 0.01, 48000))

    assert len(first) + len(second) >= 1


def test_an_empty_block_is_ignored() -> None:
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    assert resampler.push(b"") == []


# --------------------------------------------------------------------------------------
# Retention and lifecycle
# --------------------------------------------------------------------------------------


def test_reset_discards_buffered_audio() -> None:
    """A partial frame is dropped rather than padded. Silence nobody spoke is not audio."""
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)
    resampler.push(tone(440.0, 0.002, 48000))

    resampler.reset()

    assert resampler.pending_bytes == 0


def test_the_buffer_stays_bounded_across_many_blocks() -> None:
    """Held audio is at most a frame; it must not accumulate for the length of a session."""
    resampler = Resampler(source_rate_hz=44100, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    for _ in range(200):
        resampler.push(tone(440.0, 0.02, 44100))

    assert resampler.pending_bytes < FRAME_BYTES


def test_repr_carries_no_audio() -> None:
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)
    resampler.push(tone(440.0, 0.05, 48000))

    rendered = repr(resampler)

    assert "48000->16000" in rendered
    assert "pending=" in rendered


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "frame"),
    [(0, 16000, 640), (48000, 0, 640), (-1, 16000, 640)],
)
def test_a_nonsensical_rate_is_refused(source: int, target: int, frame: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        Resampler(source_rate_hz=source, target_rate_hz=target, frame_bytes=frame)


@pytest.mark.parametrize("frame_bytes", [0, -640, 641])
def test_a_frame_size_that_is_not_whole_int16_samples_is_refused(frame_bytes: int) -> None:
    with pytest.raises(ValueError, match="frame_bytes"):
        Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=frame_bytes)


def test_malformed_input_becomes_a_typed_error() -> None:
    """An odd byte count is not a whole int16 sample. It must not reach the caller raw."""
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    with pytest.raises(AudioDeviceError, match="could not resample"):
        resampler.push(b"\x01\x02\x03")


def test_the_candidate_list_covers_what_hardware_actually_offers() -> None:
    """Measured on the reference machine: the analog inputs offer 44.1 and 48 kHz."""
    assert 48000 in CANDIDATE_RATES
    assert 44100 in CANDIDATE_RATES
