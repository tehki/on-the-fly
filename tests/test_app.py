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
from on_the_fly.app.cli import build_parser, main
from on_the_fly.domain.audio import (
    DEFAULT_HANGOVER_MS,
    DEFAULT_MAX_UTTERANCE_MS,
    DEFAULT_MIN_UTTERANCE_MS,
    DEFAULT_PRE_ROLL_MS,
    CaptureStats,
    EndReason,
    SegmenterConfig,
)
from on_the_fly.domain.retention import EphemeralStore, ReapReport
from on_the_fly.infrastructure.audio import WavFileSource, WavSourceError
from on_the_fly.infrastructure.audio.microphone import DEFAULT_FRAME_MS

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
# A truncated file
#
# A WAV header states its own payload length. Nothing used to check that claim against what
# the file handed over, so a recording cut short — a recorder that crashed, a copy that
# stopped, a download that ended early — read as a shorter recording. Every duration this
# project prints is computed from the audio that arrived, so they all agree with each other
# and not one of them can notice what is missing.
# ======================================================================================


def cut_short(path: Path, samples: list[int], *, keep: int) -> Path:
    """Write `samples`, then lop the tail off the payload leaving the header untouched."""
    write_wav(path, samples)
    raw = path.read_bytes()
    header_bytes = len(raw) - len(samples) * 2
    path.write_bytes(raw[: header_bytes + keep * 2])
    return path


def test_an_intact_file_reports_no_loss(tmp_path: Path) -> None:
    source = WavFileSource(write_wav(tmp_path / "whole.wav", samples_of(3.0, 1000)))
    list(source.frames())

    assert not source.is_truncated
    assert source.truncated_seconds == 0.0
    assert source.declared_seconds == pytest.approx(3.0)
    assert source.delivered_seconds == pytest.approx(3.0)


def test_half_a_recording_missing_is_reported(tmp_path: Path) -> None:
    path = cut_short(tmp_path / "cut.wav", samples_of(3.0, 1000), keep=int(RATE * 1.5))
    source = WavFileSource(path)
    list(source.frames())

    assert source.is_truncated
    assert source.declared_seconds == pytest.approx(3.0)
    assert source.delivered_seconds == pytest.approx(1.5)
    assert source.truncated_seconds == pytest.approx(1.5)


def test_a_trailing_partial_frame_is_not_truncation(tmp_path: Path) -> None:
    """`frames()` discards a part-frame on purpose, and that decision is not a damaged file.

    A 20ms frame is 320 samples at 16 kHz, so a file one sample short of a whole number of
    frames delivers less than it declares and must still say nothing.
    """
    source = WavFileSource(write_wav(tmp_path / "odd.wav", samples_of(3.0, 1000)[:-1]))
    list(source.frames())

    assert source.delivered_seconds < source.declared_seconds
    assert not source.is_truncated


def test_one_whole_frame_missing_is_truncation(tmp_path: Path) -> None:
    """The boundary the check turns on, asserted from the far side of it."""
    samples = samples_of(3.0, 1000)
    path = cut_short(tmp_path / "cut.wav", samples, keep=len(samples) - 320)
    source = WavFileSource(path)
    list(source.frames())

    assert source.is_truncated
    assert source.truncated_seconds == pytest.approx(0.02)


def test_nothing_is_claimed_before_the_file_has_been_read(tmp_path: Path) -> None:
    """Audio not yet read is not audio that went missing."""
    path = cut_short(tmp_path / "cut.wav", samples_of(3.0, 1000), keep=int(RATE * 1.5))
    source = WavFileSource(path)

    assert not source.is_truncated
    assert source.truncated_seconds == 0.0


def test_stopping_early_does_not_look_like_truncation(tmp_path: Path) -> None:
    """A caller that reads two frames and closes has lost nothing."""
    source = WavFileSource(write_wav(tmp_path / "whole.wav", samples_of(3.0, 1000)))
    frames = source.frames()
    next(frames)
    next(frames)
    frames.close()

    assert not source.is_truncated


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


# ======================================================================================
# The numbers a finished run reports
#
# `real_time_factor` and `retention_clean` are printed and, in the second case, decide the
# exit code. Mutation testing found their comparisons could be flipped without a test
# noticing, because every existing case had both clauses failing together.
# ======================================================================================


def result_with(
    *, audio: float = 1.0, wall: float = 1.0, remaining: int = 0, reap: ReapReport | None = None
) -> PipelineResult:
    return PipelineResult(
        utterances=(),
        capture=CaptureStats(audio_seconds_seen=audio),
        wall_seconds=wall,
        final_reap=reap if reap is not None else ReapReport(),
        entries_remaining=remaining,
    )


def test_a_run_that_saw_no_audio_reports_no_pace() -> None:
    """Zero is the boundary of the guard. A run that read no frames has no pace, and a
    number here would be one nobody measured."""
    assert result_with(audio=0.0, wall=5.0).real_time_factor == 0.0
    assert result_with(audio=2.0, wall=1.0).real_time_factor == pytest.approx(0.5)

    # The guard is on zero, not on "less than a second". A clip shorter than one second
    # still has a pace, and a run that fell behind on one must say so.
    assert result_with(audio=0.5, wall=1.0).real_time_factor == pytest.approx(2.0)


def test_content_left_in_the_store_is_not_clean_even_after_a_successful_reap() -> None:
    """The clause that can fail on its own, which is the `keep_store` path.

    A reap that reported no failures says nothing about what was never due for deletion.
    Content still held at the end of a run is content retained past the point anyone
    needed it — and this property is what the command line turns into an exit code.
    """
    assert result_with(remaining=1).final_reap.ok, "the reap itself was clean"
    assert not result_with(remaining=1).retention_clean
    assert result_with(remaining=0).retention_clean


def test_a_failed_deletion_is_not_clean_even_with_nothing_left_in_the_index() -> None:
    """And the other clause on its own: the store is empty and a location refused."""
    assert not result_with(remaining=0, reap=ReapReport(failed=("entry",))).retention_clean
    assert not result_with(remaining=0, reap=ReapReport(pending_retry=("entry",))).retention_clean


# ======================================================================================
# The command line's defaults are the domain's defaults
#
# They were duplicated literals: `--frame-ms 20` written out four times, `--hangover-ms 500`
# twice, and each of the segmentation timings once more, beside a domain constant holding
# the same number. A default changed in one place and not the other is a command line
# quietly doing something the domain no longer does — and the help text is another copy of
# the same number again.
# ======================================================================================


@pytest.mark.parametrize(
    ("command", "option", "constant"),
    [
        ("segment", "pre_roll_ms", DEFAULT_PRE_ROLL_MS),
        ("segment", "hangover_ms", DEFAULT_HANGOVER_MS),
        ("segment", "min_utterance_ms", DEFAULT_MIN_UTTERANCE_MS),
        ("segment", "max_utterance_ms", DEFAULT_MAX_UTTERANCE_MS),
        ("transcribe", "hangover_ms", DEFAULT_HANGOVER_MS),
    ],
)
def test_a_segmentation_default_is_the_domains(command: str, option: str, constant: int) -> None:
    parsed = build_parser().parse_args([command, "recording.wav"])

    assert getattr(parsed, option) == constant


@pytest.mark.parametrize("command", ["segment", "transcribe", "stream", "listen"])
def test_every_command_takes_its_frame_size_from_one_place(command: str) -> None:
    """Four subcommands, one number. Three of them agreeing and the fourth not would be a
    difference nobody chose, visible only as a different frame count in the output."""
    argv = [command] if command == "listen" else [command, "recording.wav"]

    assert build_parser().parse_args(argv).frame_ms == DEFAULT_FRAME_MS


def test_the_frame_size_a_file_is_read_at_is_the_one_it_is_segmented_at() -> None:
    """`--frame-ms` is handed to the WAV reader *and* to the segmenter, which hold their own
    defaults. They are two numbers that have to be the same one."""
    assert DEFAULT_FRAME_MS == SegmenterConfig().frame_ms


def test_the_help_text_states_the_default_it_actually_uses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The number appears in prose as well, and prose does not move when a constant does.

    Read the way a user reads it, off `--help`, rather than out of argparse's internals.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["segment", "--help"])

    assert f"default: {DEFAULT_FRAME_MS}" in capsys.readouterr().out


# ---------------------------------------------------------------------------------------
# What a file that is not a WAV is told (ADR 0046)
#
# "does not start with RIFF id" is true and useless. Somebody pointing this at an m4a needs
# to know what it looks like and the one command that converts it.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "head", "expected"),
    [
        ("interview.m4a", b"\x00\x00\x00\x20ftypM4A ", "MP4 container"),
        ("song.mp3", b"ID3\x04\x00\x00", "MP3"),
        ("stream.mp3", b"\xff\xfb\x90\x00", "MP3"),
        ("voice.ogg", b"OggS\x00\x02\x00\x00", "Ogg container"),
        ("master.flac", b"fLaC\x00\x00\x00\x22", "FLAC"),
        ("old.aiff", b"FORM\x00\x00\x00\x00", "AIFF"),
        ("clip.webm", b"\x1aE\xdf\xa3\x00\x00\x00\x00", "Matroska"),
    ],
)
def test_a_recording_in_another_container_is_named_and_the_fix_is_given(
    tmp_path: Path, name: str, head: bytes, expected: str
) -> None:
    path = tmp_path / name
    path.write_bytes(head + b"\x00" * 64)

    with pytest.raises(WavSourceError) as raised:
        WavFileSource(path)

    message = str(raised.value)
    assert expected in message
    assert f"ffmpeg -i {name}" in message
    assert "-ar 16000" in message, "the conversion must land on the rate the models take"


def test_a_riff_file_that_is_not_wave_says_which_half_is_wrong(tmp_path: Path) -> None:
    path = tmp_path / "video.avi"
    path.write_bytes(b"RIFF\x00\x00\x00\x00AVI LIST" + b"\x00" * 32)

    with pytest.raises(WavSourceError, match="RIFF container but not WAVE"):
        WavFileSource(path)


def test_a_file_of_no_known_kind_still_says_what_this_reads(tmp_path: Path) -> None:
    """Guessing wrongly would be worse than not guessing, so an unknown file gets the rule
    rather than a diagnosis."""
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello there, this is not audio at all")

    with pytest.raises(WavSourceError, match="takes WAV only"):
        WavFileSource(path)


def test_the_file_commands_all_offer_the_conversion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`segment`, `transcribe` and `stream` read files. A flag on one of them and not the
    others is a flag a user finds by accident."""
    parser = build_parser()

    for command in ("segment", "transcribe", "stream"):
        args = parser.parse_args([command, "x.wav", "--resample"])

        assert args.resample is True, f"{command} does not offer --resample"


def test_a_file_already_at_the_right_rate_is_not_put_through_the_resampler(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Converting 16 kHz to 16 kHz is a filter applied for nothing, and a line of output
    claiming a conversion that did not happen."""
    path = write_wav(tmp_path / "a.wav", samples_of(1.0, 9000), rate=16_000)

    assert main(["segment", str(path), "--resample"]) == 0

    assert "resampled" not in capsys.readouterr().out


def test_a_file_at_another_rate_says_what_it_converted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Out loud, because ADR 0046's whole argument is that the caller chose this."""
    path = write_wav(tmp_path / "a.wav", [0] * 44_100, rate=44_100)

    assert main(["segment", str(path), "--resample"]) == 0

    assert "resampled     44100 Hz -> 16000 Hz" in capsys.readouterr().out


def test_without_the_flag_the_rate_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal `WavFileSource` documents stays the default: a file is exposed at its own
    rate unless somebody says otherwise."""
    path = write_wav(tmp_path / "a.wav", [0] * 44_100, rate=44_100)

    assert main(["segment", str(path)]) == 0

    output = capsys.readouterr().out
    assert "44100 Hz" in output
    assert "resampled" not in output
