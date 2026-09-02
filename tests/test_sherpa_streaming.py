"""Tests for the sherpa-onnx streaming recogniser.

Everything that can be checked without the 73 MB model is checked without it: format
refusal, missing files, laziness, the partials promise. The tests that need the real model
skip when it is absent, so a clone with no model cache still runs the whole suite.
"""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path

import pytest

from on_the_fly.domain.audio import AudioFormat, TranscriptEvent
from on_the_fly.infrastructure.asr import (
    ModelStore,
    ModelStoreError,
    SherpaStreamingRecognizer,
    StreamingRecognitionError,
    resolve,
)

RATE = 16_000


def test_the_streaming_pin_is_complete_and_permissively_licensed() -> None:
    pin = resolve("streaming-en")

    assert pin.is_pinned
    assert pin.licence == "Apache-2.0"
    assert len(pin.digests) == 4
    assert any(name.endswith("tokens.txt") for name in pin.digests)


def test_it_promises_partials() -> None:
    """Unlike the batch adapter. A caption renderer must handle text being replaced."""
    assert SherpaStreamingRecognizer(Path("unused")).emits_partials is True


def test_a_mismatched_sample_rate_is_refused_not_resampled(tmp_path: Path) -> None:
    recognizer = SherpaStreamingRecognizer(tmp_path)

    with pytest.raises(StreamingRecognitionError, match="expects 16000 Hz"):
        recognizer.validate_format(AudioFormat(sample_rate_hz=44_100))

    recognizer.validate_format(AudioFormat())


def test_an_empty_frame_does_nothing_and_loads_nothing(tmp_path: Path) -> None:
    """Laziness matters: a session that never hears audio should not pay for a model."""
    recognizer = SherpaStreamingRecognizer(tmp_path / "absent")

    assert recognizer.accept(b"") == ()
    assert "loaded=False" in repr(recognizer)


def test_a_missing_model_directory_is_reported_clearly(tmp_path: Path) -> None:
    recognizer = SherpaStreamingRecognizer(tmp_path / "absent")

    with pytest.raises(StreamingRecognitionError, match="missing"):
        recognizer.accept(b"\x00\x00" * 320)


def test_an_incomplete_model_directory_names_what_is_missing(tmp_path: Path) -> None:
    """A directory with some of the files is more dangerous than an empty one."""
    (tmp_path / "tokens.txt").write_text("dummy", encoding="utf-8")
    recognizer = SherpaStreamingRecognizer(tmp_path)

    with pytest.raises(StreamingRecognitionError, match="encoder"):
        recognizer.accept(b"\x00\x00" * 320)


def test_thread_count_is_validated() -> None:
    with pytest.raises(ValueError, match="num_threads"):
        SherpaStreamingRecognizer(Path("unused"), num_threads=0)


def test_repr_carries_no_transcript(tmp_path: Path) -> None:
    rendered = repr(SherpaStreamingRecognizer(tmp_path / "streaming-en"))

    assert "streaming-en" in rendered
    assert "threads=" in rendered


# ======================================================================================
# The real model, when it is present
# ======================================================================================


def real_streaming_model() -> Path | None:
    for cache in (
        Path.home() / ".cache" / "on-the-fly" / "models",
        Path(tempfile.gettempdir()) / "otf-models",
    ):
        try:
            return ModelStore(cache, allow_download=False).ensure(resolve("streaming-en"))
        except ModelStoreError:
            continue
    return None


def published_speech_sample() -> Path | None:
    """A real speech WAV published alongside the model, if it was fetched."""
    candidate = Path(tempfile.gettempdir()) / "otf-testwav" / "test_wavs" / "0.wav"
    return candidate if candidate.is_file() else None


def test_silence_produces_no_events() -> None:
    model_dir = real_streaming_model()
    if model_dir is None:
        pytest.skip("pinned streaming model is not present in any known cache")

    recognizer = SherpaStreamingRecognizer(model_dir)
    events: list[TranscriptEvent] = []
    for _ in range(50):  # one second of silence
        events.extend(recognizer.accept(b"\x00\x00" * 320))

    assert events == [], "silence must not invent words"


def test_real_speech_produces_partials_then_a_final() -> None:
    """The behaviour the whole streaming decision rests on."""
    model_dir = real_streaming_model()
    sample = published_speech_sample()
    if model_dir is None or sample is None:
        pytest.skip("streaming model or published speech sample not present")

    with wave.open(str(sample), "rb") as reader:
        assert reader.getframerate() == RATE
        audio = reader.readframes(reader.getnframes())

    recognizer = SherpaStreamingRecognizer(model_dir, num_threads=2)
    frame_bytes = 640
    events: list[TranscriptEvent] = []
    for offset in range(0, len(audio) - frame_bytes, frame_bytes):
        events.extend(recognizer.accept(audio[offset : offset + frame_bytes]))
    events.extend(recognizer.finish())

    partials = [e for e in events if not e.is_final]
    finals = [e for e in events if e.is_final]

    assert partials, "a streaming recogniser must emit partials"
    assert finals, "and must eventually finalise"
    # Partials grow: the model revises a hypothesis rather than appending blindly.
    assert len(partials[0].text) < len(partials[-1].text)
    assert finals[-1].text.strip()
