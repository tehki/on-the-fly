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
import wave
from pathlib import Path

import pytest

from on_the_fly.infrastructure.audio.backend import AudioDeviceError
from on_the_fly.infrastructure.audio.resampled_source import ResampledSource
from on_the_fly.infrastructure.audio.resampling import CANDIDATE_RATES, Resampler
from on_the_fly.infrastructure.audio.wav_source import WavFileSource, WavSourceError

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


@pytest.mark.parametrize("source_rate", [48000, 44100, 32000, 22050])
def test_two_seconds_in_is_two_seconds_out(source_rate: int) -> None:
    """The assertion nobody had written: *how much* audio comes out.

    Every other test here pushes one large block, where the amount is right by accident.
    A real device delivers 20 ms at a time, and that is the path that was broken: the
    resampler appended `bytes(plane)` — the plane's allocated buffer, padded for alignment
    and reused at the largest size it had needed — rather than the samples in it. It emitted
    **1.19x** the audio it was given, the surplus being stale audio from earlier blocks.

    The tolerance is one-sided in spirit: a little short is the resampler's own filter delay,
    still inside libswresample when the stream ends. Long means invented audio.
    """
    resampler = Resampler(source_rate_hz=source_rate, target_rate_hz=16000, frame_bytes=FRAME_BYTES)
    block_seconds, blocks = 0.02, 100

    emitted = 0
    for _ in range(blocks):
        for frame in resampler.push(tone(440.0, block_seconds, source_rate)):
            emitted += len(frame) // 2
    emitted += resampler.pending_bytes // 2

    expected = int(block_seconds * blocks * 16000)
    assert emitted <= expected * 1.01, "audio was invented; the surplus is padding, not sound"
    assert emitted >= expected * 0.97, "too much audio was lost to leave only filter delay"


def test_a_tone_pushed_in_device_sized_blocks_keeps_its_pitch() -> None:
    """The same guarantee as the single-block test, on the path hardware actually uses.

    Padding appended between blocks does not merely add duration — it interleaves stale
    audio with live audio, which a pitch measurement over the whole stream can survive by
    averaging. This one is here so the block path has a content check of its own.
    """
    resampler = Resampler(source_rate_hz=44100, target_rate_hz=16000, frame_bytes=FRAME_BYTES)

    frames: list[bytes] = []
    for _ in range(100):
        frames.extend(resampler.push(tone(440.0, 0.02, 44100)))
    converted = b"".join(frames)

    assert converted
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
    """Nothing is dropped at a block boundary; a word split across reads survives.

    This used to assert that two 10 ms blocks produced a whole frame, **and it passed only
    because of the padding bug**: 20 ms of input is 32 bytes short of a 20 ms frame, because
    the resampler holds a constant one-millisecond filter delay. The surplus that closed the
    gap was padding, not audio.

    So it now asserts the guarantee the name always claimed. Across every block, what has
    been emitted plus what is held tracks the input to within that delay — never less, which
    would be audio going missing, and never more, which would be audio being invented.
    """
    resampler = Resampler(source_rate_hz=48000, target_rate_hz=16000, frame_bytes=FRAME_BYTES)
    filter_delay_bytes = 64  # 1 ms at 16 kHz is 32 bytes; twice that is ample slack.

    emitted = 0
    for pushed in range(1, 4):
        for frame in resampler.push(tone(440.0, 0.01, 48000)):
            emitted += len(frame)
        ideal = int(pushed * 0.01 * 16000) * 2
        held = emitted + resampler.pending_bytes
        assert held >= ideal - filter_delay_bytes, "audio went missing at a block boundary"
        assert held <= ideal, "audio was invented at a block boundary"

    assert emitted >= FRAME_BYTES, "and a whole frame is out by the third block"


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


# --------------------------------------------------------------------------------------
# The boundaries
#
# This module emitted 1.19x the audio it was given once, because a plane's padding was read
# as samples. The count that goes out is decided by two guards and one comparison, and
# mutation testing found none of them pinned at the point they turn on.
# --------------------------------------------------------------------------------------


def test_a_buffer_of_exactly_one_frame_is_drained() -> None:
    """`>=`, not `>`. A whole frame held back because it is not more than a frame would
    delay every frame by one, for the length of the stream."""
    resampler = Resampler(source_rate_hz=16_000, target_rate_hz=16_000, frame_bytes=FRAME_BYTES)

    frames = resampler.push(b"\x00" * FRAME_BYTES)

    assert len(frames) == 1
    assert resampler.pending_bytes == 0


def test_a_buffer_one_sample_short_of_a_frame_is_held() -> None:
    """And the other side of it: a partial frame is audio, not a frame."""
    resampler = Resampler(source_rate_hz=16_000, target_rate_hz=16_000, frame_bytes=FRAME_BYTES)

    assert resampler.push(b"\x00" * (FRAME_BYTES - 2)) == []
    assert resampler.pending_bytes == FRAME_BYTES - 2


def test_exactly_two_frames_come_out_as_two() -> None:
    """The drain is a loop, and a loop that stopped after one would leave a frame behind
    on every block — which is how a buffer grows without bound."""
    resampler = Resampler(source_rate_hz=16_000, target_rate_hz=16_000, frame_bytes=FRAME_BYTES)

    assert len(resampler.push(b"\x00" * (FRAME_BYTES * 2))) == 2
    assert resampler.pending_bytes == 0


def test_the_smallest_legal_frame_is_one_sample() -> None:
    """Two bytes is one int16 sample. The guard is against zero, negative and odd counts,
    and must not refuse the smallest whole one."""
    assert Resampler(source_rate_hz=16_000, target_rate_hz=16_000, frame_bytes=2) is not None


def test_a_rate_of_one_hertz_is_allowed() -> None:
    """Absurd and legal, as it is for AudioFormat: the guard is against zero and below."""
    assert Resampler(source_rate_hz=1, target_rate_hz=1, frame_bytes=FRAME_BYTES) is not None


def test_a_zero_target_rate_is_refused() -> None:
    """Both rates are guarded, not only the source: converting *to* nothing is not a
    conversion, and the arithmetic downstream would divide by it."""
    with pytest.raises(ValueError, match="positive"):
        Resampler(source_rate_hz=48_000, target_rate_hz=0, frame_bytes=FRAME_BYTES)


def test_the_resampler_is_built_once_and_reused() -> None:
    """Rebuilding per block would restart the filter's state on every buffer and put a
    discontinuity at each boundary — audible, and invisible to a frame count."""
    resampler = Resampler(source_rate_hz=48_000, target_rate_hz=16_000, frame_bytes=FRAME_BYTES)
    resampler.push(tone(440.0, 0.05, 48_000))
    first = resampler._resampler

    resampler.push(tone(440.0, 0.05, 48_000))

    assert resampler._resampler is first, "the converter was rebuilt between blocks"


def test_reset_releases_the_converter_so_a_new_stream_starts_clean() -> None:
    """A device reopened at a different rate must not inherit the last one's filter."""
    resampler = Resampler(source_rate_hz=48_000, target_rate_hz=16_000, frame_bytes=FRAME_BYTES)
    resampler.push(tone(440.0, 0.05, 48_000))
    assert resampler._resampler is not None

    resampler.reset()

    assert resampler._resampler is None


# ---------------------------------------------------------------------------------------
# A file at the rate the models take, when the caller asks (ADR 0046)
#
# `WavFileSource` refuses to resample and is right to. This is the caller deciding, out
# loud, using the same path every microphone has gone through since ADR 0013.
# ---------------------------------------------------------------------------------------


def tone_wav(path: Path, *, rate: int, seconds: float = 1.0, hz: int = 220) -> Path:
    count = int(rate * seconds)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(
            struct.pack(
                f"<{count}h",
                *(int(9000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(count)),
            )
        )
    return path


def test_it_yields_frames_at_the_target_rate(tmp_path: Path) -> None:
    source = ResampledSource(WavFileSource(tone_wav(tmp_path / "a.wav", rate=44100)))

    frames = list(source.frames())

    assert source.audio_format.sample_rate_hz == 16_000
    assert source.source_rate_hz == 44_100
    assert {len(frame) for frame in frames} == {640}, "20 ms of 16 kHz mono int16"


def test_a_second_of_audio_comes_out_as_about_a_second(tmp_path: Path) -> None:
    """Give or take the partial frame still in the buffer, which is the design."""
    source = ResampledSource(WavFileSource(tone_wav(tmp_path / "a.wav", rate=44100)))

    seconds = sum(len(frame) for frame in source.frames()) / 2 / 16_000

    assert seconds == pytest.approx(1.0, abs=0.03)


def test_the_tone_survives_the_conversion(tmp_path: Path) -> None:
    """The point of using libswresample rather than arithmetic: a 220 Hz tone downsampled
    without an anti-aliasing filter comes back as something else, quietly."""
    source = ResampledSource(WavFileSource(tone_wav(tmp_path / "a.wav", rate=44100, hz=220)))

    audio = b"".join(source.frames())

    assert dominant_frequency(audio, 16_000) == pytest.approx(220, abs=8)


def test_it_refuses_to_decide_which_voice_to_keep(tmp_path: Path) -> None:
    """Mixing channels is a decision about content, not a conversion, so it is refused
    rather than performed."""
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(struct.pack("<4h", 1, 1, 2, 2))

    with pytest.raises((ValueError, WavSourceError)):
        ResampledSource(WavFileSource(path))


def test_it_reports_the_file_underneath_rather_than_itself(tmp_path: Path) -> None:
    """A run names the recording. The wrapper is an implementation detail of reading it."""
    path = tone_wav(tmp_path / "recording.wav", rate=44100)
    source = ResampledSource(WavFileSource(path))

    assert source.path == path
    assert not source.is_truncated


def test_closing_it_closes_the_file(tmp_path: Path) -> None:
    """A closed `WavFileSource` refuses to be read again, which is how this is observed."""
    inner = WavFileSource(tone_wav(tmp_path / "a.wav", rate=44100))
    source = ResampledSource(inner)
    list(source.frames())

    source.close()

    with pytest.raises(WavSourceError, match="closed"):
        list(inner.frames())
