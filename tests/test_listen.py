"""Tests for the live-microphone command.

`listen` is the only way to exercise the capture path without a GUI toolkit, so what is
asserted here is the wiring around the device rather than recognition itself: that nothing
opens a microphone before the arguments have been refused, that a stop is a stop and not a
failure, that the settling decorator sits under the level monitor (ADR 0020), and that the
run reports what a live capture can lose and a file cannot.

A fake source stands in for the microphone. Real hardware is exercised by hand, and on
2026-09-06 it recognised live speech for the first time — thirty seconds read aloud,
transcribed substantially correctly and translated.
"""

from __future__ import annotations

from array import array
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from on_the_fly.app.cli import main, parse_device
from on_the_fly.domain.audio import AudioFormat, EndReason, TranscriptEvent

SAMPLES_PER_FRAME = 320


def quiet(value: int = 400) -> bytes:
    """One frame of centred, speech-like audio."""
    return array("h", [value if i % 2 else -value for i in range(SAMPLES_PER_FRAME)]).tobytes()


def rail() -> bytes:
    """One frame of the power-up transient: pinned at the negative rail."""
    return array("h", [-32768] * SAMPLES_PER_FRAME).tobytes()


class FakeMicrophone:
    """An `AudioSource` with the parts of `MicrophoneSource` the command reports on."""

    def __init__(
        self,
        frames: list[bytes] | None = None,
        *,
        overflow_count: int = 0,
        capture_rate_hz: int = 16_000,
        raises: BaseException | None = None,
        device: int | str | None = None,
        frame_ms: int = 20,
    ) -> None:
        self._frames = frames if frames is not None else [quiet()] * 40
        self._raises = raises
        self.overflow_count = overflow_count
        self.capture_rate_hz = capture_rate_hz
        self.device = device
        self.frame_ms = frame_ms
        self.closed = False

    @property
    def audio_format(self) -> AudioFormat:
        return AudioFormat()

    @property
    def is_resampling(self) -> bool:
        return self.capture_rate_hz != AudioFormat().sample_rate_hz

    def frames(self) -> Iterator[bytes]:
        for frame in self._frames:
            if self.closed:
                return
            yield frame
        if self._raises is not None:
            raise self._raises

    def close(self) -> None:
        self.closed = True


class FakeStreamer:
    """Emits a partial then a final, on fixed frame counts."""

    def __init__(self) -> None:
        self.frames = 0
        self.warmed = False
        self.silent_endpoints = 2

    def warm_up(self) -> None:
        self.warmed = True

    def validate_format(self, audio_format: AudioFormat) -> None:
        return None

    def accept(self, frame: bytes) -> Sequence[TranscriptEvent]:
        self.frames += 1
        if self.frames == 15:
            return (self._event("hello", is_final=False),)
        if self.frames == 20:
            return (self._event("hello there", is_final=True),)
        return ()

    def finish(self) -> Sequence[TranscriptEvent]:
        return ()

    def _event(self, text: str, *, is_final: bool) -> TranscriptEvent:
        return TranscriptEvent(
            utterance_index=0,
            text=text,
            is_final=is_final,
            audio_offset_seconds=self.frames * 0.02,
            latency_seconds=0.0,
            duration_seconds=8.16 if is_final else None,
            end_reason=EndReason.MAX_DURATION if is_final else None,
        )

    def reset(self) -> None:
        return None


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Stand in for the model store, the recogniser, and the device."""
    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    built: dict[str, object] = {"microphone": None, "streamer": FakeStreamer()}

    def build_microphone(**kwargs: object) -> FakeMicrophone:
        microphone = FakeMicrophone(
            frames=built.pop("frames", None),  # type: ignore[arg-type]
            overflow_count=int(built.pop("overflows", 0)),  # type: ignore[call-overload]
            capture_rate_hz=int(built.pop("capture_rate", 16_000)),  # type: ignore[call-overload]
            raises=built.pop("raises", None),  # type: ignore[arg-type]
            device=kwargs.get("device"),  # type: ignore[arg-type]
        )
        built["microphone"] = microphone
        return microphone

    monkeypatch.setattr("on_the_fly.app.cli.ModelStore.ensure", lambda self, pin: model_dir)
    monkeypatch.setattr(
        "on_the_fly.app.cli.SherpaStreamingRecognizer", lambda *a, **k: built["streamer"]
    )
    monkeypatch.setattr("on_the_fly.app.cli.MicrophoneSource", build_microphone)
    return built


def test_it_listens_and_reports(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["listen", "--seconds", "30", "--cache-dir", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "English (en, streaming)" in output
    assert "hello there" in output
    assert "captured at 16000 Hz" in output
    assert "retention     clean" in output


def test_no_real_time_factor_is_reported(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Live audio arrives in real time by definition; the ratio would say nothing."""
    main(["listen", "--cache-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert "real-time" not in output
    assert "keeps up" not in output


def test_lost_audio_is_reported_rather_than_hidden(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    patched["overflows"] = 3

    main(["listen", "--cache-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert "dropped       3 overflow(s) - audio was lost" in output


def test_a_clean_run_says_nothing_was_lost(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A silent zero would read as an absent measurement rather than a good one."""
    main(["listen", "--cache-dir", str(tmp_path)])

    assert "dropped       none" in capsys.readouterr().out


def test_the_power_up_transient_is_discarded_before_recognition(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0020: the recogniser must not be handed the rail-pinned start of a session."""
    patched["frames"] = [rail()] * 25 + [quiet()] * 40
    streamer = patched["streamer"]
    assert isinstance(streamer, FakeStreamer)

    main(["listen", "--cache-dir", str(tmp_path)])

    output = capsys.readouterr().out
    # 25 frames of rail, then one 240 ms window before it can be called settled.
    assert "settling      740ms discarded" in output
    assert streamer.frames == 65 - 37


def test_the_level_verdict_is_reported_after_settling(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The transient must not be what the user is told about their microphone."""
    patched["frames"] = [rail()] * 25 + [quiet()] * 40

    main(["listen", "--cache-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert "input         ok" in output
    assert "turn its input gain down" not in output


def test_resampling_is_stated(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    patched["capture_rate"] = 48_000

    main(["listen", "--cache-dir", str(tmp_path)])

    assert "captured at 48000 Hz, resampled to 16000 Hz" in capsys.readouterr().out


def test_ctrl_c_is_a_stop_and_not_a_failure(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The summary is the point of the run, so an interrupt must still produce one."""
    patched["raises"] = KeyboardInterrupt()

    exit_code = main(["listen", "--cache-dir", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "stopped        by Ctrl-C" in output
    assert "retention     clean" in output


def test_the_device_is_released_on_every_path(patched: dict[str, object], tmp_path: Path) -> None:
    patched["raises"] = KeyboardInterrupt()

    main(["listen", "--cache-dir", str(tmp_path)])

    microphone = patched["microphone"]
    assert isinstance(microphone, FakeMicrophone)
    assert microphone.closed


def test_a_refusable_argument_is_refused_before_the_microphone_is_opened(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Holding a microphone open to discover the language is wrong is a privacy problem."""
    exit_code = main(["listen", "--language", "zz", "--cache-dir", str(tmp_path)])

    assert exit_code == 1
    assert patched["microphone"] is None
    assert "zz" in capsys.readouterr().err


def test_a_pair_with_no_pinned_model_is_refused_before_the_microphone_is_opened(
    patched: dict[str, object], tmp_path: Path
) -> None:
    exit_code = main(["listen", "--translate-to", "de", "--cache-dir", str(tmp_path)])

    assert exit_code == 1
    assert patched["microphone"] is None


def test_a_nonsensical_duration_is_refused(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(["listen", "--seconds", "0", "--cache-dir", str(tmp_path)])

    assert exit_code == 1
    assert "--seconds must be positive" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("given", "expected"),
    [(None, None), ("13", 13), ("0", 0), ("Built-in Audio", "Built-in Audio")],
)
def test_a_device_is_an_index_or_a_name(given: str | None, expected: int | str | None) -> None:
    """Both, because an index is what a user's audio settings show them and a name is not."""
    assert parse_device(given) == expected


def test_the_device_argument_reaches_the_adapter(
    patched: dict[str, object], tmp_path: Path
) -> None:
    main(["listen", "--device", "13", "--cache-dir", str(tmp_path)])

    microphone = patched["microphone"]
    assert isinstance(microphone, FakeMicrophone)
    assert microphone.device == 13


def test_a_final_says_how_long_it_ran_and_what_stopped_it(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reading a recording cannot give, and the one this command was missing."""
    main(["listen", "--cache-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert "(8.16s MAX_DURATION)" in output


def test_endpoints_that_decoded_nothing_are_reported(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Otherwise a silent room and a broken endpointer produce identical output."""
    main(["listen", "--cache-dir", str(tmp_path)])

    output = capsys.readouterr().out
    assert "endpoints     1 with text, 2 with none (silence)" in output


def test_a_recogniser_that_cannot_count_endpoints_is_not_required_to(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The port does not require it; only this implementation happens to offer it."""
    streamer = patched["streamer"]
    assert isinstance(streamer, FakeStreamer)
    del streamer.silent_endpoints

    exit_code = main(["listen", "--cache-dir", str(tmp_path)])

    assert exit_code == 0
    assert "endpoints " not in capsys.readouterr().out


def test_partials_are_shown_while_somebody_is_still_speaking(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole argument for a streaming recogniser is that text exists before the speaker
    has finished (ADR 0006). A run that showed only finals would be a slower batch run."""
    assert main(["listen", "--seconds", "30", "--cache-dir", str(tmp_path)]) == 0

    assert "partial]" in capsys.readouterr().out, "no partial caption was printed"


def test_finals_only_hides_them_and_keeps_the_text(
    patched: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """For somebody reading over a shoulder rather than watching a caption rewrite itself."""
    assert main(["listen", "--seconds", "30", "--cache-dir", str(tmp_path), "--finals-only"]) == 0

    output = capsys.readouterr().out
    # The caption lines, not the summary — which counts partials whether or not they were
    # shown, and should keep doing so.
    assert "partial]" not in output
    assert "1 partial, 1 final" in output
    assert "hello there" in output, "the finals went with the partials"


def test_a_translated_listen_reports_how_many_of_how_many(
    patched: dict[str, object],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counter is incremented in the live loop, and nothing asserted it there: a
    mutation starting it at one, or never adding to it, survived every test."""
    monkeypatch.setattr("on_the_fly.app.cli.open_translator", lambda *a, **k: FixedTranslator())

    assert (
        main(
            [
                "listen",
                "--seconds",
                "30",
                "--cache-dir",
                str(tmp_path),
                "--translate-to",
                "ru",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "→ переведено" in output, "the translation is not marked as one"
    assert "translation   1 of 1 final(s)" in output


class FixedTranslator:
    """Answers instantly, so the summary line has something to report."""

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        return "переведено"
