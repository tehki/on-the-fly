"""Tests for the streaming path through the composition root and the command line.

A fake streaming recogniser stands in for sherpa-onnx so the wiring — event flow, transcript
retention, language refusal, cleanup — is asserted in milliseconds and without a 73 MB model.
"""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from on_the_fly.app import StreamingRun
from on_the_fly.app.cli import main
from on_the_fly.domain import languages
from on_the_fly.domain.audio import AudioFormat, TranscriptEvent
from on_the_fly.domain.languages import Language, RecognitionTier
from on_the_fly.infrastructure.audio import WavFileSource

RATE = 16_000


def speech_wav(path: Path, seconds: float = 1.0) -> Path:
    count = int(RATE * seconds)
    samples = [int(8000 * math.sin(2 * math.pi * 220 * i / RATE)) for i in range(count)]
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


class FakeStreamer:
    """Emits two partials then a final, on fixed frame counts."""

    def __init__(self) -> None:
        self.frames = 0
        self.reset_calls = 0
        self.warmed = False

    def warm_up(self) -> None:
        self.warmed = True

    def validate_format(self, audio_format: AudioFormat) -> None:
        return None

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        self.frames += 1
        if self.frames == 5:
            return (self._event("hello", is_final=False),)
        if self.frames == 10:
            return (self._event("hello there", is_final=False),)
        if self.frames == 20:
            return (self._event("hello there friend", is_final=True),)
        return ()

    def finish(self) -> Sequence[TranscriptEvent]:
        return ()

    def reset(self) -> None:
        self.reset_calls += 1

    def _event(self, text: str, *, is_final: bool) -> TranscriptEvent:
        return TranscriptEvent(
            utterance_index=1,
            text=text,
            is_final=is_final,
            audio_offset_seconds=0.0,
            latency_seconds=0.0,
        )


class CountingSource:
    """A source that records how often it was closed."""

    def __init__(self, frames: int = 40) -> None:
        self._frames = [bytes(640)] * frames
        self.closed = 0

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat()

    def frames(self) -> Iterator[bytes]:
        yield from self._frames

    def close(self) -> None:
        self.closed += 1


def test_the_source_is_released_when_the_stream_ends() -> None:
    """A microphone left open after a session is a privacy problem, not just a leak.

    Added after mutation testing showed the suite passed with the close() removed — the
    batch path had this covered and the streaming path did not.
    """
    source = CountingSource()
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    assert source.closed >= 1


def test_the_source_is_released_when_the_caller_walks_away() -> None:
    source = CountingSource(frames=200)
    run = StreamingRun(source, FakeStreamer())

    generator = run.events()
    next(generator)
    generator.close()

    assert source.closed >= 1, "abandoning the stream must still release the device"


def test_events_are_yielded_as_they_appear(tmp_path: Path) -> None:
    """Yielded, not returned. A streaming result that arrives at the end is not streaming."""
    source = WavFileSource(speech_wav(tmp_path / "a.wav"))
    run = StreamingRun(source, FakeStreamer())

    events = list(run.events())

    assert [e.text for e in events] == ["hello", "hello there", "hello there friend"]
    assert [e.is_final for e in events] == [False, False, True]


def test_finals_are_stored_and_partials_are_not(tmp_path: Path) -> None:
    """A trail of half-sentences would retain more content than the finished text."""
    source = WavFileSource(speech_wav(tmp_path / "a.wav"))
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    assert len(run.final_handles) == 1, "one final stored, both partials dropped"


def test_the_store_is_purged_and_the_source_closed(tmp_path: Path) -> None:
    source = WavFileSource(speech_wav(tmp_path / "a.wav"))
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    stats = run.stats
    assert stats is not None
    assert stats.retention_clean
    assert stats.entries_remaining == 0
    assert len(run.store) == 0


def test_stats_report_pace_and_first_text(tmp_path: Path) -> None:
    source = WavFileSource(speech_wav(tmp_path / "a.wav", seconds=2.0))
    run = StreamingRun(source, FakeStreamer())

    list(run.events())

    stats = run.stats
    assert stats is not None
    assert stats.frames_read == 100
    assert stats.audio_seconds == pytest.approx(2.0, abs=0.05)
    assert stats.partials == 2
    assert stats.finals == 1
    assert stats.first_text_after_seconds is not None
    assert stats.keeps_up, "a fake recogniser must comfortably keep up"


def test_abandoning_the_stream_still_cleans_up(tmp_path: Path) -> None:
    """The caller stops consuming; the source and the store are still released."""
    source = WavFileSource(speech_wav(tmp_path / "a.wav", seconds=3.0))
    run = StreamingRun(source, FakeStreamer())

    generator = run.events()
    next(generator)
    generator.close()

    stats = run.stats
    assert stats is not None, "cleanup must run even when the caller walks away"
    assert stats.retention_clean
    assert len(run.store) == 0


# ======================================================================================
# Command line
# ======================================================================================


def patch_streaming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeStreamer:
    fake = FakeStreamer()
    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: model_dir)
    monkeypatch.setattr("on_the_fly.app.cli.SherpaStreamingRecognizer", lambda *a, **k: fake)
    return fake


def test_the_cli_streams_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_wav(tmp_path / "a.wav")
    fake = patch_streaming(monkeypatch, tmp_path)

    exit_code = main(["stream", str(path), "--cache-dir", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "English (en, streaming)" in output
    assert "hello there friend" in output
    assert "keeps up" in output
    assert "retention     clean" in output
    assert fake.warmed, "the model is loaded before the clock starts"


def test_finals_only_hides_partials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_wav(tmp_path / "a.wav")
    patch_streaming(monkeypatch, tmp_path)

    main(["stream", str(path), "--cache-dir", str(tmp_path), "--finals-only"])

    output = capsys.readouterr().out
    assert "hello there friend" in output
    # The summary line legitimately counts partials, so assert on the event marker rather
    # than the word: no partial event was rendered.
    assert "partial]" not in output
    assert "final  ]" in output


def test_a_batch_only_language_is_refused_not_downgraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently giving batch latency to someone who asked to stream is worse than refusing.

    Tajik used to supply this case; ADR 0010 removed it, and no shipped language is BATCH
    today. The guard in the CLI is still live, so the language is injected rather than the
    test deleted — a control that stops being exercised is a control on its way out.
    """
    path = speech_wav(tmp_path / "a.wav")
    batch_only = Language(
        "xx",
        "Example",
        RecognitionTier.BATCH,
        note="no streaming model exists",
    )
    monkeypatch.setitem(languages.SUPPORTED, "xx", batch_only)

    exit_code = main(["stream", str(path), "--language", "xx", "--cache-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not a streaming language" in captured.err
    assert "transcribe" in captured.err, "the error must say what to use instead"


def test_a_removed_language_is_refused_outright(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0010: Tajik is gone, and asking for it fails closed rather than guessing."""
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--language", "tg", "--cache-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unsupported language" in captured.err


def test_a_streaming_language_with_no_pinned_model_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """German streams in principle; nobody has pinned a model for it yet."""
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--language", "de", "--cache-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "no pinned streaming model" in captured.err
    assert "pin_model.py" in captured.err


def test_an_unknown_language_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = speech_wav(tmp_path / "a.wav")

    exit_code = main(["stream", str(path), "--language", "zh", "--cache-dir", str(tmp_path)])

    assert exit_code == 1
    assert "unsupported language" in capsys.readouterr().err
