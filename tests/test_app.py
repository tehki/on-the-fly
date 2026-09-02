"""Tests for the WAV source, the composition root, and the command line.

This is the layer that makes the pipeline runnable, so these tests are the first that
exercise capture, segmentation and retention together over real audio rather than scripted
frames. The audio is synthesised into a temporary file per test: deterministic, no fixture
binaries in the repository, and nothing to clean up.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import pytest

from on_the_fly.app import PipelineResult, run_capture
from on_the_fly.app.cli import main
from on_the_fly.domain.audio import EndReason, SegmenterConfig
from on_the_fly.domain.retention import EphemeralStore
from on_the_fly.infrastructure.audio import WavFileSource, WavSourceError

RATE = 16_000
FRAME_MS = 20


def samples_of(seconds: float, amplitude: int, freq: float = 220.0) -> list[int]:
    """A tone, or silence when amplitude is zero."""
    count = int(RATE * seconds)
    if amplitude == 0:
        return [0] * count
    return [int(amplitude * math.sin(2 * math.pi * freq * i / RATE)) for i in range(count)]


def write_wav(
    path: Path,
    samples: list[int],
    *,
    channels: int = 1,
    sample_width: int = 2,
    rate: int = RATE,
) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(rate)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def speech_like(path: Path) -> Path:
    """Silence, a burst, silence, a shorter burst, silence — two clear utterances."""
    samples = (
        samples_of(0.4, 0)
        + samples_of(1.2, 9000)
        + samples_of(0.8, 0)
        + samples_of(0.9, 9000)
        + samples_of(0.6, 0)
    )
    return write_wav(path, samples)


# ======================================================================================
# WavFileSource
# ======================================================================================


def test_reads_fixed_size_frames(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", samples_of(1.0, 5000))
    source = WavFileSource(path, frame_ms=FRAME_MS)

    frames = list(source.frames())

    assert source.audio_format.sample_rate_hz == RATE
    assert len(frames) == 50, "one second at 20ms frames"
    assert all(len(frame) == 640 for frame in frames)
    assert source.frames_yielded == 50


def test_a_partial_trailing_frame_is_discarded_not_padded(tmp_path: Path) -> None:
    """Padding invents audio; a fraction of a frame carries nothing worth keeping."""
    # 50 whole frames plus 100 leftover samples.
    path = write_wav(tmp_path / "ragged.wav", samples_of(1.0, 5000) + [0] * 100)
    source = WavFileSource(path, frame_ms=FRAME_MS)

    frames = list(source.frames())

    assert len(frames) == 50
    assert all(len(frame) == 640 for frame in frames)


def test_stereo_and_wide_samples_are_refused(tmp_path: Path) -> None:
    """Downmixing silently is a quality decision nobody made."""
    stereo = write_wav(tmp_path / "stereo.wav", samples_of(0.1, 1000) * 2, channels=2)
    with pytest.raises(WavSourceError, match="mono"):
        WavFileSource(stereo)


def test_a_missing_file_and_a_directory_are_refused(tmp_path: Path) -> None:
    with pytest.raises(WavSourceError, match="no such file"):
        WavFileSource(tmp_path / "absent.wav")
    with pytest.raises(WavSourceError, match="not a regular file"):
        WavFileSource(tmp_path)


def test_a_non_wav_file_is_refused(tmp_path: Path) -> None:
    """A file is untrusted input; the header is validated before any audio is used."""
    impostor = tmp_path / "not-really.wav"
    impostor.write_bytes(b"this is not a wav file at all")
    with pytest.raises(WavSourceError, match="not a readable WAV"):
        WavFileSource(impostor)


def test_allowed_root_confines_the_path(tmp_path: Path) -> None:
    """Traversal is compared after resolution, so ../ is already collapsed."""
    root = tmp_path / "inbox"
    root.mkdir()
    outside = write_wav(tmp_path / "outside.wav", samples_of(0.1, 1000))

    with pytest.raises(WavSourceError, match="allowed root"):
        WavFileSource(outside, allowed_root=root)

    with pytest.raises(WavSourceError, match="allowed root"):
        WavFileSource(root / ".." / "outside.wav", allowed_root=root)

    inside = write_wav(root / "inside.wav", samples_of(0.1, 1000))
    assert WavFileSource(inside, allowed_root=root).path == inside.resolve()


def test_a_closed_source_refuses_to_read_again(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", samples_of(0.1, 1000))
    source = WavFileSource(path)
    list(source.frames())

    with pytest.raises(WavSourceError, match="closed"):
        list(source.frames())


def test_reading_twice_at_once_is_refused(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", samples_of(1.0, 1000))
    source = WavFileSource(path)
    first = source.frames()
    next(first)

    with pytest.raises(WavSourceError, match="already being read"):
        list(source.frames())

    first.close()


def test_close_is_idempotent_and_repr_carries_no_audio(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "tone.wav", samples_of(0.1, 9000))
    source = WavFileSource(path)
    list(source.frames())
    source.close()
    source.close()

    rendered = repr(source)
    assert "tone.wav" in rendered
    assert "\\x" not in rendered


# ======================================================================================
# Composition root
# ======================================================================================


def test_a_run_segments_audio_and_leaves_nothing_retained(tmp_path: Path) -> None:
    """Capture, segmentation and retention exercised together over real audio."""
    source = WavFileSource(speech_like(tmp_path / "speech.wav"), frame_ms=FRAME_MS)

    result = run_capture(source)

    assert len(result.utterances) == 2, "two bursts should produce two utterances"
    assert all(u.ended_because is EndReason.SILENCE for u in result.utterances)
    assert result.utterances[0].duration_seconds > 1.0
    assert result.audio_seconds == pytest.approx(3.9, abs=0.05)

    # The claim that matters: the run ended holding nothing.
    assert result.entries_remaining == 0
    assert result.final_reap.ok
    assert result.retention_clean


def test_a_run_over_silence_produces_no_utterances(tmp_path: Path) -> None:
    source = WavFileSource(write_wav(tmp_path / "quiet.wav", samples_of(2.0, 0)))

    result = run_capture(source)

    assert result.utterances == ()
    assert result.retention_clean
    assert result.capture.frames_read == 100


class UndeletableLocation:
    """A spill location that never manages to delete anything."""

    @property
    def location(self) -> str:
        return "broken_spill"

    def delete(self, entry_id: str) -> None:
        raise OSError("device busy")

    def purge_all(self) -> None:
        return None


def test_a_run_that_cannot_delete_reports_unclean_retention(tmp_path: Path) -> None:
    """The failure path of the claim the CLI exits on.

    Added after mutation testing showed the suite passed with `retention_clean` hardcoded
    to True — every test happened to exercise only the clean case, so the property that
    decides the exit code was never actually checked.
    """
    source = WavFileSource(speech_like(tmp_path / "speech.wav"))
    store = EphemeralStore("on-the-fly", deleters=[UndeletableLocation()])

    result = run_capture(source, store=store)

    assert result.utterances, "audio was still segmented"
    assert not result.final_reap.ok
    assert result.entries_remaining > 0
    assert result.retention_clean is False, "a run that could not delete is not clean"


def test_the_cli_exits_nonzero_when_retention_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that could not delete what it held is not a successful run."""
    path = speech_like(tmp_path / "speech.wav")

    def failing_run(source: object, **kwargs: object) -> PipelineResult:
        store = EphemeralStore("on-the-fly", deleters=[UndeletableLocation()])
        return run_capture(source, store=store, **kwargs)  # type: ignore[arg-type]

    # Patched by name: the cli module does not re-export run_capture, and reaching
    # through it as an attribute is exactly what strict mypy objects to.
    monkeypatch.setattr("on_the_fly.app.cli.run_capture", failing_run)

    exit_code = main(["segment", str(path)])

    assert exit_code == 3, "retention failure gets its own exit code"
    assert "retention     FAILED" in capsys.readouterr().out


def test_real_time_factor_is_reported(tmp_path: Path) -> None:
    """The first measurable number this project has. Segmentation only."""
    source = WavFileSource(speech_like(tmp_path / "speech.wav"))

    result = run_capture(source)

    assert result.wall_seconds > 0
    assert 0 < result.real_time_factor < 1.0, "segmentation must keep up with speech"


def test_a_tighter_segmenter_config_is_honoured(tmp_path: Path) -> None:
    source = WavFileSource(speech_like(tmp_path / "speech.wav"), frame_ms=FRAME_MS)
    config = SegmenterConfig(
        frame_ms=FRAME_MS,
        pre_roll_ms=40,
        hangover_ms=40,
        min_utterance_ms=100,
        max_utterance_ms=500,
    )

    result = run_capture(source, config=config)

    assert len(result.utterances) > 2, "a 500ms ceiling should cut the bursts up"
    # The invariant is the bound, not the reason. Each burst is chopped at the ceiling
    # until its tail, which ends on silence like any other utterance — so a mix of
    # MAX_DURATION and SILENCE is the correct outcome, and asserting only MAX_DURATION
    # would be asserting a coincidence.
    assert any(u.ended_because is EndReason.MAX_DURATION for u in result.utterances)
    assert all(u.duration_seconds <= 0.5 + 1e-9 for u in result.utterances), (
        "no utterance may exceed the configured maximum"
    )
    assert result.retention_clean


# ======================================================================================
# Command line
# ======================================================================================


def test_cli_reports_utterances_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_like(tmp_path / "speech.wav")

    exit_code = main(["segment", str(path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "2 utterance(s):" in output
    assert "retention     clean" in output
    assert "16000 Hz mono 16-bit" in output


def test_cli_json_output_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_like(tmp_path / "speech.wav")

    exit_code = main(["segment", str(path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["retention_clean"] is True
    assert payload["entries_remaining"] == 0
    assert len(payload["utterances"]) == 2
    assert payload["sample_rate_hz"] == RATE
    # Metadata only: no field here could carry what was said.
    assert set(payload["utterances"][0]) == {
        "index",
        "start_seconds",
        "duration_seconds",
        "frame_count",
        "ended_because",
    }


def test_cli_reports_a_bad_file_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["segment", str(tmp_path / "absent.wav")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err


def test_cli_enforces_the_allowed_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "inbox"
    root.mkdir()
    outside = speech_like(tmp_path / "outside.wav")

    exit_code = main(["segment", str(outside), "--allowed-root", str(root)])

    assert exit_code == 1
    assert "allowed root" in capsys.readouterr().err


def test_cli_rejects_a_nonsensical_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = speech_like(tmp_path / "speech.wav")

    exit_code = main(
        ["segment", str(path), "--min-utterance-ms", "9000", "--max-utterance-ms", "1000"]
    )

    assert exit_code == 1
    assert "max_utterance_ms must exceed" in capsys.readouterr().err
